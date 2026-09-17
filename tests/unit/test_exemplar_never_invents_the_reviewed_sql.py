"""少样本示例只能是人确认过的那条 SQL，不能拿有损投影现编一条冒充。

现场（2026-09-17）：「账户余额大于 1000 的有多少人」试问答对，28。用户点「存为评测
用例」，再跑评测就变成 1.0，六道治理关全绿。

追下去：试问那次模型写的是 CTE ——按账号求平均余额，再数出平均余额大于 1000 的账号。
`semantic_query` 投影表达不了 CTE、表达不了外层的 COUNT、也表达不了那个落在派生别名上的
过滤，于是存下来的「期望」只剩「avg 账户余额 按 账号」，三类过滤全空，
`expected_s2sql` 是 None（界面从不传它，尽管响应里就有 `corrected_s2sql`）。

真正的伤害不在评测：`parser.py` 渲染示例时写的是
``item.s2sql or serialize_s2sql(item.semantic_query, release)``——没有权威 SQL 就拿投影
现编一条，当作「人工确认的示例」喂给模型。这条示例与原问题字字相同，相似度永远最高，
必被选中，于是模型照着编出来的错示例写。存一条答对的用例，反而教会模型答错，而且
`recall()` 在正常问数里也跑。

`contracts.py` 早把规则写死了：自然语言路径的权威是 corrected_s2sql，
`semantic_query` 这个 DTO **must never be fed back into the textual Translator path**。

判据要零成本且确定：投影会产出「维度数 + 指标数」列，人确认过的 `expected_rows` 有几列
是事实。两者对不上，就证明这份投影描述的不是被确认的那个答案，不配当示例。
（不是充要条件——列数对得上仍可能丢了过滤；所以存端必须同时开始存 `corrected_s2sql`，
这道判据只是存量用例的兜底。）
"""

from __future__ import annotations

from datetime import UTC, datetime

from knowflow_analytics.contracts import Aggregation, QueryAggregationOverride
from knowflow_analytics.evaluation.contracts import GoldenCase, GoldenSuite, GoldenSuiteRecord
from knowflow_analytics.query.contracts import MemoryReviewResult, MemoryStatus, QueryState
from knowflow_analytics.query.exemplars import GoldenSuiteExemplarProvider
from knowflow_analytics.semantic.index import EmbeddingBatch


class _Catalog:
    def __init__(self, records):
        self.records = records

    def list_golden_suites(self, *, project_id: str, revision_id: str):
        return self.records


class _EmbeddingGateway:
    def for_tenant(self, _tenant_id):
        return self

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="exemplar-fidelity-test",
            dimension=2,
            vectors=tuple((1.0, 0.0) for _ in texts),
        )


def _case(
    *,
    dimensions: tuple[str, ...],
    expected_rows: tuple[tuple[object, ...], ...],
    s2sql: str | None = None,
) -> GoldenCase:
    return GoldenCase(
        id="case-balance",
        question="账户余额大于 1000 的有多少人",
        dataset_ids=("sales_dataset",),
        memory_status=MemoryStatus.ENABLED,
        memory_review_result=MemoryReviewResult.POSITIVE,
        expected_state=QueryState.COMPLETED,
        expected_dataset_id="sales_dataset",
        expected_metric_ids=("net_revenue",),
        expected_aggregation_overrides=(
            QueryAggregationOverride(metric_id="net_revenue", aggregation=Aggregation.SUM),
        ),
        expected_dimension_ids=dimensions,
        expected_s2sql=s2sql,
        expected_rows=expected_rows,
    )


def _recall(release, case: GoldenCase):
    release = release.model_copy(update={"revision_id": "revision-1"})
    record = GoldenSuiteRecord(
        id="suite-1",
        project_id=release.project_id,
        revision_id=release.revision_id,
        revision_etag=1,
        schema_snapshot_hash="sha256:schema",
        semantic_spec_hash=release.spec_hash,
        suite=GoldenSuite(
            id="suite-1", name="suite-1", project_id=release.project_id, cases=(case,)
        ),
        saved_by="reviewer",
        updated_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    provider = GoldenSuiteExemplarProvider(
        catalog=_Catalog((record,)),
        embedding_gateway=_EmbeddingGateway(),
    )
    return provider.recall(
        question="账户余额大于 1000 的有多少人",
        release=release,
        dataset_id="sales_dataset",
        limit=10,
    )


def test_a_projection_that_cannot_describe_the_reviewed_answer_is_not_an_exemplar(
    sales_release,
) -> None:
    """人确认的是一个数（28），投影却会产出两列——它描述的不是那个答案。"""

    recalled = _recall(
        sales_release,
        _case(dimensions=("region",), expected_rows=((28,),)),
    )

    assert recalled == ()


def test_the_reviewed_sql_is_used_verbatim_even_when_the_projection_is_lossy(
    sales_release,
) -> None:
    """存了权威 SQL 就不必再猜投影忠不忠实——它本来就是人看过的那条。"""

    authoritative = 'SELECT COUNT("区域") FROM "销售经营"'
    recalled = _recall(
        sales_release,
        _case(dimensions=("region",), expected_rows=((28,),), s2sql=authoritative),
    )

    assert [item.s2sql for item in recalled] == [authoritative]


def test_a_faithful_projection_still_teaches(sales_release) -> None:
    """列数对得上的存量用例照常当示例——这道兜底只拦证明不了的那些。"""

    recalled = _recall(
        sales_release,
        _case(dimensions=("region",), expected_rows=(("华东", 100),)),
    )

    assert [item.id for item in recalled] == ["suite-1:case-balance"]
