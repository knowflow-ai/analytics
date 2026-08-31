from __future__ import annotations

from knowflow_analytics.catalog.store import PublishedRelease
from knowflow_analytics.contracts import QueryResult
from knowflow_analytics.query.contracts import QueryRequest, QueryState
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic import SemanticTranslator


class _ReleaseProvider:
    def __init__(self, release, index) -> None:
        self.published = PublishedRelease(
            release=release.model_copy(update={"index_snapshot_id": index.id}),
            index_snapshot=index,
            status="active",
        )

    def get_active_release(self, _project_id):
        return self.published


class _Executor:
    def execute(self, *, query, release):
        return QueryResult(columns=("region", "net_revenue"), rows=(("华东", 300),), row_count=1)


class _BrokenSqlGateway:
    """LLM 每次都吐语法错误，迫使最终解析退回 Rule。"""

    def generate_json(self, **_kwargs):
        return {"thought": "invalid", "sql": "SELECT ("}


def _service(sales_release, sales_index, *, llm):
    return AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_BrokenSqlGateway()) if llm else None,
        ),
        translator=SemanticTranslator(),
        executor=_Executor(),
    )


def test_rule_fallback_warning_ships_even_when_diagnostics_were_not_requested(
    sales_release, sales_index
):
    """Rule fallback 给出的是一个看起来正常、可能漏掉排名/占比意图的数字。

    此前它和 info 级的"成功"诊断一起挂在 include_diagnostics（默认 False）后面，
    默认调用方对降级一无所知 —— 这正是静默降级。失败路径早就无条件返回诊断，
    理由同样适用：降级只报告一次、请求不可重放。
    """

    response = _service(sales_release, sales_index, llm=True).query(
        QueryRequest(project_id="sales", question="各区域净收入", dataset_ids=("sales_dataset",))
    )

    assert response.state is QueryState.COMPLETED
    assert response.diagnostics is not None
    assert response.diagnostics.category == "rule_fallback"
    assert response.diagnostics.severity == "warning"


def test_a_clean_success_still_keeps_the_info_diagnosis_behind_the_flag(sales_release, sales_index):
    """只放行 warning，不是把所有诊断都打开：干净成功时默认仍然不带诊断。"""

    response = _service(sales_release, sales_index, llm=False).query(
        QueryRequest(project_id="sales", question="各区域净收入", dataset_ids=("sales_dataset",))
    )

    assert response.state is QueryState.COMPLETED
    assert response.diagnostics is None


def test_the_info_diagnosis_appears_when_asked_for(sales_release, sales_index):
    response = _service(sales_release, sales_index, llm=False).query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入",
            dataset_ids=("sales_dataset",),
            include_diagnostics=True,
        )
    )

    assert response.diagnostics is not None
    assert response.diagnostics.category == "success"
