from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol

from pydantic import Field, model_validator

from knowflow_analytics.contracts import FrozenModel, SemanticRelease
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.analysis_topics import scope_canonical_names


class SemanticElementType(StrEnum):
    DATASET = "dataset"
    METRIC = "metric"
    DIMENSION = "dimension"
    DIMENSION_VALUE = "dimension_value"
    TERM = "term"


class IndexState(StrEnum):
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class SemanticIndexEntry(FrozenModel):
    id: str
    phrase: str
    normalized_phrase: str
    element_type: SemanticElementType
    element_id: str
    dataset_ids: tuple[str, ...]
    source: str
    priority: int = Field(default=100, ge=0, le=1_000)
    description: str = ""
    dimension_id: str | None = None
    raw_value: object | None = None


def index_identity_hash(
    *,
    release_spec_hash: str,
    embedding_model_id: str,
    vector_dimension: int,
    entries: Iterable[SemanticIndexEntry],
) -> str:
    """Hash the content that gives one semantic index its identity.

    Embedding vectors are deliberately excluded.  Remote embedding services are
    not bit-reproducible - the same texts sent to the same model return floats
    that differ in their last digits - so hashing them made every rebuild of an
    unchanged release mint a new snapshot id.  Confirmation memories are bound
    to that id, so a rebuild silently invalidated every stored confirmation and
    the same question asked for the same clarification again.  Identity is the
    indexed semantics plus the embedding model, not the exact floats it
    returned this time.
    """

    return content_hash(
        {
            "release_spec_hash": release_spec_hash,
            "embedding_model_id": embedding_model_id,
            "vector_dimension": vector_dimension,
            "entries": [entry.model_dump(mode="json") for entry in entries],
        }
    )


class SemanticIndexSnapshot(FrozenModel):
    id: str
    release_spec_hash: str
    content_hash: str
    state: IndexState
    embedding_model_id: str
    vector_dimension: int = Field(ge=1)
    entries: tuple[SemanticIndexEntry, ...]
    vectors: tuple[tuple[float, ...], ...]

    @model_validator(mode="after")
    def validate_vectors(self) -> SemanticIndexSnapshot:
        if len(self.entries) != len(self.vectors):
            raise ValueError("semantic entries and vectors must have equal length")
        if any(len(vector) != self.vector_dimension for vector in self.vectors):
            raise ValueError("embedding vector dimension mismatch")
        return self


class EmbeddingBatch(FrozenModel):
    model_id: str
    dimension: int = Field(ge=1)
    vectors: tuple[tuple[float, ...], ...]


class EmbeddingGateway(Protocol):
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch: ...

    def for_tenant(self, tenant_id: str) -> EmbeddingGateway: ...


class SemanticIndexBuildError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "SEMANTIC_INDEX_BUILD_FAILED") -> None:
        super().__init__(message, code=code, stage="INDEXING")


class SemanticIndexBuilder:
    def __init__(self, embedding_gateway: EmbeddingGateway) -> None:
        self._embedding_gateway = embedding_gateway

    def build(self, release: SemanticRelease) -> SemanticIndexSnapshot:
        entries = self._entries(release)
        if not entries:
            raise SemanticIndexBuildError("release produced an empty semantic index")
        # Text segment conversion embeds the item name directly. Type and
        # scope stay in metadata; prefixing the text (for example, ``metric:``)
        # moves short Chinese business terms away from their query embeddings.
        embedding_texts = tuple(entry.phrase for entry in entries)
        batch = self._embedding_gateway.encode(embedding_texts)
        if len(batch.vectors) != len(entries):
            raise SemanticIndexBuildError("embedding gateway returned an incomplete batch")
        payload_hash = index_identity_hash(
            release_spec_hash=release.spec_hash,
            embedding_model_id=batch.model_id,
            vector_dimension=batch.dimension,
            entries=entries,
        )
        return SemanticIndexSnapshot(
            id=f"idx_{payload_hash.removeprefix('sha256:')[:20]}",
            release_spec_hash=release.spec_hash,
            content_hash=payload_hash,
            state=IndexState.READY,
            embedding_model_id=batch.model_id,
            vector_dimension=batch.dimension,
            entries=entries,
            vectors=batch.vectors,
        )

    @staticmethod
    def _entries(release: SemanticRelease) -> tuple[SemanticIndexEntry, ...]:
        datasets_by_metric: dict[str, list[str]] = defaultdict(list)
        datasets_by_dimension: dict[str, list[str]] = defaultdict(list)
        entries: list[SemanticIndexEntry] = []
        seen: set[tuple[str, str, str, tuple[str, ...]]] = set()

        def append(
            *,
            phrase: str,
            element_type: SemanticElementType,
            element_id: str,
            dataset_ids: tuple[str, ...],
            source: str,
            priority: int,
            description: str = "",
            dimension_id: str | None = None,
            raw_value: object | None = None,
        ) -> None:
            normalized = normalize_text(phrase)
            if not normalized:
                return
            scope = tuple(sorted(dataset_ids))
            key = (element_type.value, element_id, normalized, scope)
            if key in seen:
                return
            seen.add(key)
            entries.append(
                SemanticIndexEntry(
                    id=f"entry_{content_hash(key).removeprefix('sha256:')[:20]}",
                    phrase=phrase,
                    normalized_phrase=normalized,
                    element_type=element_type,
                    element_id=element_id,
                    dataset_ids=scope,
                    source=source,
                    priority=priority,
                    description=description,
                    dimension_id=dimension_id,
                    raw_value=raw_value,
                )
            )

        metrics_by_id = {item.id: item for item in release.metrics}
        dimensions_by_id = {item.id: item for item in release.dimensions}
        routes_by_dataset = {
            item.dataset_id: item for item in release.analysis_topic_routes
        }
        for dataset in release.datasets:
            for phrase, source, priority in _names(
                dataset.name, dataset.aliases, technical_name=dataset.id
            ):
                append(
                    phrase=phrase,
                    element_type=SemanticElementType.DATASET,
                    element_id=dataset.id,
                    dataset_ids=(dataset.id,),
                    source=source,
                    priority=priority,
                )
            for metric_id in dataset.metric_ids:
                datasets_by_metric[metric_id].append(dataset.id)
            for dimension_id in dataset.dimension_ids:
                datasets_by_dimension[dimension_id].append(dataset.id)
            route = routes_by_dataset.get(dataset.id)
            if route is not None:
                scoped_names = scope_canonical_names(release, route)
                for metric_id in dataset.metric_ids:
                    metric = metrics_by_id[metric_id]
                    effective = scoped_names.get(metric_id, metric.name)
                    if effective != metric.name:
                        append(
                            phrase=effective,
                            element_type=SemanticElementType.METRIC,
                            element_id=metric_id,
                            dataset_ids=(dataset.id,),
                            source="scope_name",
                            priority=325,
                        )
                for dimension_id in dataset.dimension_ids:
                    dimension = dimensions_by_id[dimension_id]
                    effective = scoped_names.get(dimension_id, dimension.name)
                    if effective != dimension.name:
                        append(
                            phrase=effective,
                            element_type=SemanticElementType.DIMENSION,
                            element_id=dimension_id,
                            dataset_ids=(dataset.id,),
                            source="scope_name",
                            priority=325,
                        )

        for metric in release.metrics:
            scope = tuple(datasets_by_metric.get(metric.id, ()))
            for phrase, source, priority in _names(
                metric.name, metric.aliases, technical_name=metric.id
            ):
                append(
                    phrase=phrase,
                    element_type=SemanticElementType.METRIC,
                    element_id=metric.id,
                    dataset_ids=scope,
                    source=source,
                    priority=priority,
                )
        for dimension in release.dimensions:
            scope = tuple(datasets_by_dimension.get(dimension.id, ()))
            for phrase, source, priority in _names(
                dimension.name, dimension.aliases, technical_name=dimension.id
            ):
                append(
                    phrase=phrase,
                    element_type=SemanticElementType.DIMENSION,
                    element_id=dimension.id,
                    dataset_ids=scope,
                    source=source,
                    priority=priority,
                )
        for value in release.dimension_values:
            if not value.enabled:
                continue
            scope = tuple(datasets_by_dimension.get(value.dimension_id, ()))
            for phrase, source, priority in _names(
                value.display_name, value.aliases, technical_name=str(value.value)
            ):
                append(
                    phrase=phrase,
                    element_type=SemanticElementType.DIMENSION_VALUE,
                    element_id=value.id,
                    dataset_ids=scope,
                    source=source,
                    priority=priority,
                    dimension_id=value.dimension_id,
                    raw_value=value.value,
                )
        for term in release.terms:
            scope = term.dataset_ids or tuple(
                sorted(
                    {
                        *(
                            dataset_id
                            for metric_id in term.metric_ids
                            for dataset_id in datasets_by_metric.get(metric_id, ())
                        ),
                        *(
                            dataset_id
                            for dimension_id in term.dimension_ids
                            for dataset_id in datasets_by_dimension.get(dimension_id, ())
                        ),
                    }
                )
            )
            for phrase, source, priority in _names(term.name, term.aliases):
                append(
                    phrase=phrase,
                    element_type=SemanticElementType.TERM,
                    element_id=term.id,
                    dataset_ids=scope,
                    source=source,
                    priority=priority,
                    description=term.description,
                )
        return tuple(sorted(entries, key=lambda item: (item.element_type, item.id)))


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(normalized.split())


def _names(
    name: str,
    aliases: tuple[str, ...],
    *,
    technical_name: str | None = None,
) -> tuple[tuple[str, str, int], ...]:
    values = [(name, "name", 300)]
    values.extend((alias, "alias", 250) for alias in aliases)
    if technical_name:
        values.append((technical_name, "technical_name", 100))
    return tuple(values)
