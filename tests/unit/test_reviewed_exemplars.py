from __future__ import annotations

import ast
import random
from datetime import UTC, datetime

from knowflow_analytics.contracts import Aggregation, QueryAggregationOverride, SemanticQuery
from knowflow_analytics.evaluation.contracts import (
    GoldenCase,
    GoldenSuite,
    GoldenSuiteRecord,
)
from knowflow_analytics.query.contracts import (
    MapMode,
    MemoryReviewResult,
    MemoryStatus,
    QueryState,
)
from knowflow_analytics.query.exemplars import (
    GoldenSuiteExemplarProvider,
    ReviewedS2SqlExemplar,
    select_few_shot_exemplars,
)
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.parser import LlmS2SqlParser
from knowflow_analytics.semantic.index import EmbeddingBatch


class _Catalog:
    def __init__(self, records: tuple[GoldenSuiteRecord, ...]) -> None:
        self.records = records
        self.calls: list[tuple[str, str]] = []

    def list_golden_suites(self, *, project_id: str, revision_id: str):
        self.calls.append((project_id, revision_id))
        return self.records


class _EmbeddingGateway:
    def for_tenant(self, tenant_id):
        return self

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        self.calls.append(texts)
        vectors = tuple(_vector(text) for text in texts)
        return EmbeddingBatch(model_id="reviewed-exemplar-test", dimension=2, vectors=vectors)


class _Provider:
    def __init__(self, exemplars: tuple[ReviewedS2SqlExemplar, ...]) -> None:
        self.exemplars = exemplars
        self.calls = []

    def recall(self, **kwargs):
        self.calls.append(kwargs)
        return self.exemplars[: kwargs["limit"]]


class _CapturingGateway:
    def __init__(self) -> None:
        self.request = None
        self.requests = []

    def generate_json(self, **kwargs):
        self.request = kwargs
        self.requests.append(kwargs)
        return {
            "thought": "使用审核样例",
            "sql": 'SELECT SUM("净收入") FROM "销售经营"',
        }


def test_provider_recalls_only_reviewed_release_bound_non_holdout_cases(sales_release) -> None:
    release = sales_release.model_copy(update={"revision_id": "revision-1"})
    records = (
        _record(
            release,
            suite_id="reviewed",
            cases=(
                _case("good", "各区域收入", memory_status=MemoryStatus.ENABLED).model_copy(
                    update={
                        "expected_s2sql": (
                            'SELECT MAX("净收入") AS "_最高收入_", '
                            'AVG("净收入") AS "_平均收入_" FROM "销售经营"'
                        )
                    }
                ),
                _case("pending", "尚未审核的题", memory_status=MemoryStatus.PENDING),
                _case("disabled", "已经停用的题", memory_status=MemoryStatus.DISABLED),
                _case("ordinary", "普通黄金题", tags=("regression",)),
                _case("invalid", "失效指标", memory_status=MemoryStatus.ENABLED).model_copy(
                    update={
                        "expected_metric_ids": ("not_published",),
                        "expected_aggregation_overrides": (
                            QueryAggregationOverride(
                                metric_id="not_published",
                                aggregation=Aggregation.SUM,
                            ),
                        ),
                    }
                ),
            ),
        ),
        _record(
            release.model_copy(update={"spec_hash": "another-spec"}),
            suite_id="another-release",
            cases=(_case("other", "其他版本", memory_status=MemoryStatus.ENABLED),),
        ),
    )
    catalog = _Catalog(records)
    provider = GoldenSuiteExemplarProvider(
        catalog=catalog,
        embedding_gateway=_EmbeddingGateway(),
    )

    recalled = provider.recall(
        question="区域净收入",
        release=release,
        dataset_id="sales_dataset",
        limit=10,
    )

    assert catalog.calls == [("sales", "revision-1")]
    assert [item.id for item in recalled] == ["reviewed:good"]
    assert recalled[0].semantic_query.metric_ids == ("net_revenue",)
    assert recalled[0].semantic_query.aggregation_overrides == (
        QueryAggregationOverride(metric_id="net_revenue", aggregation=Aggregation.SUM),
    )
    assert recalled[0].s2sql == (
        'SELECT MAX("净收入") AS "_最高收入_", AVG("净收入") AS "_平均收入_" FROM "销售经营"'
    )
    serialized = recalled[0].model_dump_json()
    assert "expected_rows" not in serialized
    assert "physical_sql" not in serialized
    assert "尚未审核的题" not in serialized
    assert "已经停用的题" not in serialized


def test_provider_requires_an_immutable_release_revision(sales_release) -> None:
    catalog = _Catalog(())
    provider = GoldenSuiteExemplarProvider(
        catalog=catalog,
        embedding_gateway=_EmbeddingGateway(),
    )

    assert (
        provider.recall(
            question="区域净收入",
            release=sales_release,
            dataset_id="sales_dataset",
            limit=10,
        )
        == ()
    )
    assert catalog.calls == []


def test_provider_caches_release_bound_exemplar_vectors(sales_release) -> None:
    release = sales_release.model_copy(update={"revision_id": "revision-1"})
    catalog = _Catalog(
        (
            _record(
                release,
                suite_id="reviewed",
                cases=(_case("good", "各区域收入", memory_status=MemoryStatus.ENABLED),),
            ),
        )
    )
    embedding_gateway = _EmbeddingGateway()
    provider = GoldenSuiteExemplarProvider(
        catalog=catalog,
        embedding_gateway=embedding_gateway,
    )

    for question in ("区域净收入", "区域销售额"):
        assert provider.recall(
            question=question,
            release=release,
            dataset_id="sales_dataset",
            limit=10,
        )

    assert embedding_gateway.calls[0] == ("各区域收入",)
    assert embedding_gateway.calls[1:] == [("区域净收入",), ("区域销售额",)]


def test_provider_evicts_stale_exemplar_vector_snapshots(sales_release) -> None:
    release = sales_release.model_copy(update={"revision_id": "revision-1"})
    catalog = _Catalog(
        (
            _record(
                release,
                suite_id="reviewed",
                cases=(_case("first", "审核样例一", memory_status=MemoryStatus.ENABLED),),
            ),
        )
    )
    embedding_gateway = _EmbeddingGateway()
    provider = GoldenSuiteExemplarProvider(
        catalog=catalog,
        embedding_gateway=embedding_gateway,
        vector_cache_capacity=1,
    )

    assert provider.recall(
        question="第一次查询",
        release=release,
        dataset_id="sales_dataset",
        limit=10,
    )
    catalog.records = (
        _record(
            release,
            suite_id="reviewed",
            cases=(_case("second", "审核样例二", memory_status=MemoryStatus.ENABLED),),
        ),
    )
    assert provider.recall(
        question="第二次查询",
        release=release,
        dataset_id="sales_dataset",
        limit=10,
    )
    catalog.records = (
        _record(
            release,
            suite_id="reviewed",
            cases=(_case("first", "审核样例一", memory_status=MemoryStatus.ENABLED),),
        ),
    )
    assert provider.recall(
        question="第三次查询",
        release=release,
        dataset_id="sales_dataset",
        limit=10,
    )

    assert embedding_gateway.calls.count(("审核样例一",)) == 2


def test_prompt_helper_selection_keeps_exact_and_most_similar_examples() -> None:
    exemplars = tuple(
        _exemplar(f"e{index}", similarity)
        for index, similarity in enumerate((0.55, 0.65, 0.75, 0.85, 0.995))
    )

    selected = select_few_shot_exemplars(
        exemplars,
        few_shot_number=3,
        randomizer=random.Random(7),
    )

    assert len(selected) == 3
    assert exemplars[-1] in selected  # similarity > 0.989 is mandatory upstream
    assert exemplars[-2] in selected  # most similar non-exact exemplar is mandatory


def test_prompt_helper_bounds_an_all_exact_exemplar_set_without_crashing() -> None:
    exemplars = tuple(_exemplar(f"exact-{index}", 0.999) for index in range(5))

    selected = select_few_shot_exemplars(
        exemplars,
        few_shot_number=3,
        randomizer=random.Random(3),
    )

    assert len(selected) == 3
    assert all(item.similarity > 0.989 for item in selected)


def test_llm_parser_injects_three_reviewed_exemplars_without_results_or_physical_sql(
    sales_release,
    sales_index,
) -> None:
    exemplars = tuple(_exemplar(f"e{index}", 0.5 + index / 10) for index in range(5))
    provider = _Provider(exemplars)
    gateway = _CapturingGateway()
    parser = LlmS2SqlParser(
        gateway,
        exemplar_provider=provider,
        randomizer=random.Random(11),
    )
    mapping = SemanticMapper().map(
        question="净收入",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )

    parser.parse(
        question="净收入",
        release=sales_release.model_copy(update={"revision_id": "revision-1"}),
        mapping=mapping,
        query_id="reviewed-exemplar-prompt",
    )

    assert provider.calls[0]["limit"] == 10
    user_content = gateway.request["messages"][1]["content"]
    exemplar_line = next(
        line for line in user_content.splitlines() if line.startswith("reviewed_exemplars=")
    )
    prompt_exemplars = ast.literal_eval(exemplar_line.removeprefix("reviewed_exemplars="))
    assert len(prompt_exemplars) == 3
    assert all(set(item) == {"question", "schema", "side_info", "sql"} for item in prompt_exemplars)
    assert all(item["schema"]["dataset"] == "销售经营" for item in prompt_exemplars)
    assert all("net_revenue" not in item["sql"] for item in prompt_exemplars)
    assert "expected_rows" not in user_content
    assert "physical_sql" not in user_content


def test_llm_parser_reuses_the_same_exemplars_for_the_all_retry(
    sales_release,
    sales_index,
) -> None:
    exemplars = tuple(_exemplar(f"e{index}", 0.5 + index / 10) for index in range(5))
    provider = _Provider(exemplars)
    gateway = _CapturingGateway()
    parser = LlmS2SqlParser(
        gateway,
        exemplar_provider=provider,
        randomizer=random.Random(17),
    )
    release = sales_release.model_copy(update={"revision_id": "revision-1"})
    strict_mapping = SemanticMapper().map(
        question="净收入",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )
    all_mapping = SemanticMapper().map(
        question="净收入",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.ALL,
    )

    for mapping in (strict_mapping, all_mapping):
        parser.parse(
            question="净收入",
            release=release,
            mapping=mapping,
            query_id="same-query",
        )

    assert len(provider.calls) == 1
    exemplar_lines = [
        next(
            line
            for line in request["messages"][1]["content"].splitlines()
            if line.startswith("reviewed_exemplars=")
        )
        for request in gateway.requests
    ]
    assert exemplar_lines[0] == exemplar_lines[1]


def _case(
    case_id: str,
    question: str,
    *,
    tags: tuple[str, ...] = (),
    memory_status: MemoryStatus = MemoryStatus.DISABLED,
) -> GoldenCase:
    return GoldenCase(
        id=case_id,
        question=question,
        dataset_ids=("sales_dataset",),
        tags=tags,
        memory_status=memory_status,
        memory_review_result=(
            MemoryReviewResult.POSITIVE if memory_status is MemoryStatus.ENABLED else None
        ),
        expected_state=QueryState.COMPLETED,
        expected_dataset_id="sales_dataset",
        expected_metric_ids=("net_revenue",),
        expected_aggregation_overrides=(
            QueryAggregationOverride(metric_id="net_revenue", aggregation=Aggregation.SUM),
        ),
        expected_rows=((100,),),
    )


def _record(
    release,
    *,
    suite_id: str,
    cases: tuple[GoldenCase, ...],
) -> GoldenSuiteRecord:
    return GoldenSuiteRecord(
        id=suite_id,
        project_id=release.project_id,
        revision_id=release.revision_id,
        revision_etag=1,
        schema_snapshot_hash="sha256:schema",
        semantic_spec_hash=release.spec_hash,
        suite=GoldenSuite(
            id=suite_id,
            name=suite_id,
            project_id=release.project_id,
            cases=cases,
        ),
        saved_by="reviewer",
        updated_at=datetime(2026, 8, 19, tzinfo=UTC),
    )


def _exemplar(exemplar_id: str, similarity: float) -> ReviewedS2SqlExemplar:
    return ReviewedS2SqlExemplar(
        id=exemplar_id,
        question=f"问题-{exemplar_id}",
        semantic_query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            aggregation_overrides=(
                QueryAggregationOverride(
                    metric_id="net_revenue",
                    aggregation=Aggregation.SUM,
                ),
            ),
        ),
        similarity=similarity,
    )


def _vector(text: str) -> tuple[float, float]:
    if "区域" in text:
        return (1.0, 0.0)
    if "收入" in text:
        return (0.9, 0.1)
    return (0.0, 1.0)


def test_recall_survives_float_cosine_overflow(sales_release) -> None:
    """问题与已审核样例文本完全相同时,查询向量与样例向量一致,浮点余弦可算出
    1.0000000000000002;召回必须钳回 [-1, 1],不能让整次问数被 500 打断。"""

    release = sales_release.model_copy(update={"revision_id": "revision-1"})
    records = (
        _record(
            release,
            suite_id="reviewed",
            cases=(_case("good", "华东净收入共多少", memory_status=MemoryStatus.ENABLED),),
        ),
    )

    # cos(v, v) == 1.0000000000000002:纯浮点误差,不是构造出来的病态输入。
    overflow = (0.854, 0.361, 0.053, 0.741)

    class _OverflowGateway:
        def for_tenant(self, tenant_id):
            return self

        def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
            return EmbeddingBatch(
                model_id="overflow-test",
                dimension=4,
                vectors=tuple(overflow for _ in texts),
            )

    provider = GoldenSuiteExemplarProvider(
        catalog=_Catalog(records),
        embedding_gateway=_OverflowGateway(),
    )

    recalled = provider.recall(
        question="华东净收入共多少",
        release=release,
        dataset_id="sales_dataset",
        limit=10,
    )

    assert [item.id for item in recalled] == ["reviewed:good"]
    assert recalled[0].similarity == 1.0
