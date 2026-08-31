from __future__ import annotations

import math
import random
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Protocol

from pydantic import Field

from knowflow_analytics.contracts import (
    FrozenModel,
    QueryFilter,
    SemanticQuery,
    SemanticRelease,
)
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.query.contracts import MemoryReviewResult, MemoryStatus, QueryState
from knowflow_analytics.semantic.index import EmbeddingGateway
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

if TYPE_CHECKING:
    from knowflow_analytics.evaluation.contracts import GoldenCase, GoldenSuiteRecord

EXEMPLAR_RECALL_NUMBER = 10
EXEMPLAR_FEW_SHOT_NUMBER = 3
EXEMPLAR_EXACT_SIMILARITY = 0.989
EXEMPLAR_VECTOR_CACHE_CAPACITY = 1_000


def relevance_score(cosine: float) -> float:
    """余弦相似度 → 上游口径的 relevance score。

    上游阈值比较的不是原始余弦,而是 langchain4j 的
    ``RelevanceScore.fromCosineSimilarity(cos) = (cos + 1) / 2``
    (InMemoryEmbeddingStore.java:160 → EmbeddingServiceImpl.java:157)。
    直接拿 0.9 / 0.989 去比原始余弦会把门槛整整抬高 0.1 cosine,
    业务同义词因此被卡在召回之外。
    """

    return (cosine + 1.0) / 2.0

_EVALUATION_ONLY_TAGS = frozenset({"holdout", "calibration"})


class ReviewedS2SqlExemplar(FrozenModel):
    """Human-reviewed logical S2SQL example bound to an immutable release."""

    id: str = Field(min_length=1, max_length=256)
    question: str = Field(min_length=1, max_length=4_000)
    semantic_query: SemanticQuery
    s2sql: str | None = Field(default=None, min_length=1, max_length=100_000)
    similarity: float = Field(ge=-1.0, le=1.0)


class ReviewedExemplarProvider(Protocol):
    def recall(
        self,
        *,
        question: str,
        release: SemanticRelease,
        dataset_id: str,
        limit: int,
        tenant_id: str = "",
    ) -> tuple[ReviewedS2SqlExemplar, ...]: ...


class _GoldenSuiteCatalog(Protocol):
    def list_golden_suites(
        self,
        *,
        project_id: str,
        revision_id: str,
    ) -> tuple[GoldenSuiteRecord, ...]: ...


class _Randomizer(Protocol):
    def shuffle(self, values: list[ReviewedS2SqlExemplar]) -> None: ...


@dataclass(frozen=True)
class _ExemplarVectorSnapshot:
    model_id: str
    dimension: int
    vectors: tuple[tuple[float, ...], ...]


class GoldenSuiteExemplarProvider:
    """Recall explicit reviewed examples from the existing GoldenSuite authority.

    Exemplars live in an embedding collection and are recalled by question
    vector, without a second mutable review state: only GoldenSuite cases in
    ``ENABLED`` state with a
    positive human review and bound to the queried immutable Revision/spec are
    eligible.
    """

    def __init__(
        self,
        *,
        catalog: _GoldenSuiteCatalog,
        embedding_gateway: EmbeddingGateway,
        vector_cache_capacity: int = EXEMPLAR_VECTOR_CACHE_CAPACITY,
    ) -> None:
        if vector_cache_capacity < 1:
            raise ValueError("exemplar vector cache capacity must be positive")
        self._catalog = catalog
        self._embedding_gateway = embedding_gateway
        self._vector_cache: OrderedDict[tuple[str, str, str, str, str], _ExemplarVectorSnapshot] = (
            OrderedDict()
        )
        self._vector_cache_capacity = vector_cache_capacity
        self._cache_lock = Lock()

    def recall(
        self,
        *,
        question: str,
        release: SemanticRelease,
        dataset_id: str,
        limit: int,
        tenant_id: str = "",
    ) -> tuple[ReviewedS2SqlExemplar, ...]:
        if release.revision_id is None or limit <= 0:
            return ()
        records = self._catalog.list_golden_suites(
            project_id=release.project_id,
            revision_id=release.revision_id,
        )
        candidates: list[tuple[str, str, SemanticQuery, str | None]] = []
        seen: set[tuple[str, str]] = set()
        textual_translator = S2SqlSemanticTranslator()
        for record in records:
            if record.semantic_spec_hash != release.spec_hash:
                continue
            for case in record.suite.cases:
                query = _reviewed_case_query(case, dataset_id=dataset_id)
                if query is None or not _query_is_in_release(query, release=release):
                    continue
                s2sql = case.expected_s2sql
                if s2sql is not None:
                    try:
                        textual_translator.translate(
                            release=release,
                            dataset_id=dataset_id,
                            corrected_s2sql=s2sql,
                        )
                    except AnalyticsError:
                        continue
                identity = (" ".join(case.question.split()).casefold(), query.dataset_id)
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append((f"{record.id}:{case.id}", case.question, query, s2sql))
        if not candidates:
            return ()

        candidate_hash = content_hash(
            [
                {
                    "id": exemplar_id,
                    "question": question_text,
                    "semantic_query": semantic_query.model_dump(mode="json"),
                    "s2sql": s2sql,
                }
                for exemplar_id, question_text, semantic_query, s2sql in candidates
            ]
        )
        cache_key = (
            tenant_id,
            release.spec_hash,
            release.revision_id,
            dataset_id,
            candidate_hash,
        )
        embedding_gateway = self._embedding_gateway.for_tenant(tenant_id)
        with self._cache_lock:
            vector_snapshot = self._vector_cache.get(cache_key)
            if vector_snapshot is not None:
                self._vector_cache.move_to_end(cache_key)
        if vector_snapshot is None:
            exemplar_batch = embedding_gateway.encode(
                tuple(question_text for _id, question_text, _query, _s2sql in candidates)
            )
            if len(exemplar_batch.vectors) != len(candidates):
                raise ValueError("exemplar embedding response is incomplete")
            generated_snapshot = _ExemplarVectorSnapshot(
                model_id=exemplar_batch.model_id,
                dimension=exemplar_batch.dimension,
                vectors=exemplar_batch.vectors,
            )
            with self._cache_lock:
                vector_snapshot = self._vector_cache.get(cache_key)
                if vector_snapshot is None:
                    vector_snapshot = generated_snapshot
                    self._vector_cache[cache_key] = vector_snapshot
                    while len(self._vector_cache) > self._vector_cache_capacity:
                        self._vector_cache.popitem(last=False)
                else:
                    self._vector_cache.move_to_end(cache_key)
        query_batch = embedding_gateway.encode((question,))
        if (
            vector_snapshot.model_id != query_batch.model_id
            or vector_snapshot.dimension != query_batch.dimension
            or len(query_batch.vectors) != 1
        ):
            raise ValueError("exemplar embedding response is inconsistent")
        query_vector = query_batch.vectors[0]
        recalled = tuple(
            ReviewedS2SqlExemplar(
                id=exemplar_id,
                question=question_text,
                semantic_query=semantic_query,
                s2sql=s2sql,
                similarity=_cosine(query_vector, vector),
            )
            for (exemplar_id, question_text, semantic_query, s2sql), vector in zip(
                candidates,
                vector_snapshot.vectors,
                strict=True,
            )
        )
        return tuple(
            sorted(
                recalled,
                key=lambda item: (-item.similarity, item.id),
            )[:limit]
        )


def select_few_shot_exemplars(
    exemplars: Sequence[ReviewedS2SqlExemplar],
    *,
    few_shot_number: int = EXEMPLAR_FEW_SHOT_NUMBER,
    randomizer: _Randomizer = random,
) -> tuple[ReviewedS2SqlExemplar, ...]:
    """Port ``PromptHelper.getFewShotExemplars`` for one inference.

    Similarity above 0.989 is mandatory. The remaining candidates are trimmed,
    shuffled and forced to retain the most-similar non-exact example. The pinned
    Java implementation crashes when every recalled example is exact and may exceed
    ``fewShotNumber`` when exact matches alone exceed the limit; Python keeps the
    same selection order while bounding those two invalid edge cases.
    """

    if few_shot_number <= 0 or not exemplars:
        return ()
    same = [
        item for item in exemplars if relevance_score(item.similarity) > EXEMPLAR_EXACT_SIMILARITY
    ]
    no_same = [
        item for item in exemplars if relevance_score(item.similarity) <= EXEMPLAR_EXACT_SIMILARITY
    ]
    if (len(no_same) - len(same)) > few_shot_number:
        no_same.sort(key=lambda item: (item.similarity, item.id))
        no_same = no_same[(len(no_same) - few_shot_number) // 2 :]
    most_similar = max(no_same, key=lambda item: (item.similarity, item.id)) if no_same else None
    randomizer.shuffle(no_same)
    if same:
        same = sorted(same, key=lambda item: (-item.similarity, item.id))[:few_shot_number]
        need_size = min(len(no_same) + len(same), few_shot_number)
        selected = [*no_same[: max(need_size - len(same), 0)], *same]
    else:
        selected = no_same[: min(len(no_same), few_shot_number)]
        if most_similar is not None and most_similar not in selected:
            if selected:
                selected[-1] = most_similar
            else:
                selected.append(most_similar)
    return tuple(selected[:few_shot_number])


def _reviewed_case_query(case: GoldenCase, *, dataset_id: str) -> SemanticQuery | None:
    tags = set(case.tags)
    if (
        case.memory_status is not MemoryStatus.ENABLED
        or case.memory_review_result is not MemoryReviewResult.POSITIVE
        or tags.intersection(_EVALUATION_ONLY_TAGS)
        or case.expected_state is not QueryState.COMPLETED
    ):
        return None
    expected_dataset_id = case.expected_dataset_id
    if expected_dataset_id is None and len(case.dataset_ids) == 1:
        expected_dataset_id = case.dataset_ids[0]
    if expected_dataset_id != dataset_id:
        return None
    return SemanticQuery(
        dataset_id=dataset_id,
        query_type=case.expected_query_type,
        metric_ids=case.expected_metric_ids,
        aggregation_overrides=case.expected_aggregation_overrides,
        dimension_ids=case.expected_dimension_ids,
        filters=tuple(
            QueryFilter(
                dimension_id=item.dimension_id,
                operator=item.operator,
                value=item.value,
            )
            for item in case.expected_filters
        ),
        measure_filters=case.expected_measure_filters,
        metric_filters=case.expected_metric_filters,
        order_by=case.expected_order_by or (),
        limit=case.expected_limit,
    )


def _query_is_in_release(query: SemanticQuery, *, release: SemanticRelease) -> bool:
    dataset = next((item for item in release.datasets if item.id == query.dataset_id), None)
    if dataset is None:
        return False
    metrics = set(dataset.metric_ids)
    dimensions = set(dataset.dimension_ids)
    if not set(query.metric_ids).issubset(metrics):
        return False
    if not set(query.dimension_ids).issubset(dimensions):
        return False
    if not {item.metric_id for item in query.aggregation_overrides}.issubset(metrics):
        return False
    if not {item.dimension_id for item in query.filters}.issubset(dimensions):
        return False
    if not {item.metric_id for item in query.measure_filters}.issubset(metrics):
        return False
    if not {item.metric_id for item in query.metric_filters}.issubset(metrics):
        return False
    return all(
        item.element_id in metrics or item.element_id in dimensions for item in query.order_by
    )


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(sum(item * item for item in left)) * math.sqrt(
        sum(item * item for item in right)
    )
    if not denominator:
        return 0.0
    # 同向量的浮点余弦会溢出成 1.0000000000000002,越过 similarity 的 le=1 约束。
    return max(-1.0, min(1.0, numerator / denominator))
