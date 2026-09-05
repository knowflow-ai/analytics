"""点踩闭环合同（2026-09-05 用户评审）。

点赞点踩此前只往消息行写一个字符串，没有任何下游——`analytics_message.feedback`
除了列表回显没有读取方，核心完全不知道有这件事。而点踩恰恰是**静默错答唯一的
外部信号**：查询成功、六道治理关全绿、数字也出来了，只有用户知道不对。

所以它进的是「问数反馈」同一张待处理列表：前四类（refused/clarified/inferred/
unknown_value）是系统自己察觉的异常，disliked 是人告诉我们的，处理方式相同。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowflow_analytics.query.contracts import QueryFailureRecord


def _record(**overrides) -> QueryFailureRecord:
    payload = dict(
        kind="disliked",
        reason="metric",
        question="各门店销售额是多少",
        stage="FINISHED",
        code="USER_DISLIKED_ANSWER",
        release_id="rel-1",
        spec_hash="sha256:spec",
        index_snapshot_id="idx-1",
    )
    payload.update(overrides)
    return QueryFailureRecord(**payload)


class TestDislikedRecord:
    def test_a_successful_answer_can_be_recorded_as_disliked(self) -> None:
        record = _record(comment="口径应该扣掉退款")

        assert record.kind == "disliked"
        assert record.reason == "metric"
        assert record.comment == "口径应该扣掉退款"
        # 它不是失败：查询跑完了，阶段是 FINISHED。
        assert record.stage == "FINISHED"

    def test_the_other_kinds_still_default_to_no_reason(self) -> None:
        # 原有四类不带原因，默认值必须保持空，否则历史记录会凭空多出一个口径。
        assert _record(kind="refused", reason="").reason == ""
        assert (
            QueryFailureRecord(
                question="q",
                stage="PRECHECK",
                code="X",
                release_id="r",
                spec_hash="s",
                index_snapshot_id="i",
            ).kind
            == "refused"
        )

    @pytest.mark.parametrize("reason", ("scope", "metric", "value", "understanding", "other"))
    def test_every_offered_reason_is_accepted(self, reason: str) -> None:
        assert _record(reason=reason).reason == reason

    def test_an_unknown_reason_is_refused(self) -> None:
        # 原因是给建模者分流用的，自由文本会让列表退化成一堆读不完的句子。
        with pytest.raises(ValidationError):
            _record(reason="随便写点什么")

    def test_the_comment_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            _record(comment="x" * 1_001)


class _Catalog:
    def __init__(self) -> None:
        self.saved: list[tuple[QueryFailureRecord, dict]] = []

    def save_failure(self, record, *, actor_id: str, project_id: str) -> None:
        self.saved.append((record, {"actor_id": actor_id, "project_id": project_id}))


class TestApplicationEntryPoint:
    def test_a_dislike_lands_in_the_same_worklist(self) -> None:
        from knowflow_analytics.application import AnalyticsApplication

        catalog = _Catalog()
        application = AnalyticsApplication.__new__(AnalyticsApplication)
        application.catalog = catalog  # type: ignore[attr-defined]

        AnalyticsApplication.record_answer_feedback(
            application,
            project_id="prj-1",
            actor_id="user-1",
            question="各门店销售额是多少",
            liked=False,
            reason="scope",
            comment="应该只看上海",
            release_id="rel-1",
            spec_hash="sha256:spec",
            index_snapshot_id="idx-1",
            dataset_ids=("ds-1",),
        )

        record, context = catalog.saved[0]
        assert context == {"actor_id": "user-1", "project_id": "prj-1"}
        assert (record.kind, record.reason, record.code) == (
            "disliked",
            "scope",
            "USER_DISLIKED_ANSWER",
        )
        assert record.dataset_ids == ("ds-1",)


class TestLikedAnswer:
    """赞不是缺口，是一条被人确认过的问答——评测集要的正是这种样本。"""

    def test_a_like_is_recorded_without_a_reason(self) -> None:
        record = _record(kind="liked", reason="", code="USER_LIKED_ANSWER")

        assert record.kind == "liked"
        assert record.reason == ""

    def test_the_application_marks_likes_and_dislikes_apart(self) -> None:
        from knowflow_analytics.application import AnalyticsApplication

        catalog = _Catalog()
        application = AnalyticsApplication.__new__(AnalyticsApplication)
        application.catalog = catalog  # type: ignore[attr-defined]

        for liked in (True, False):
            AnalyticsApplication.record_answer_feedback(
                application,
                project_id="prj-1",
                actor_id="user-1",
                question="各门店销售额是多少",
                liked=liked,
                reason="" if liked else "metric",
                release_id="rel-1",
                spec_hash="sha256:spec",
                index_snapshot_id="idx-1",
            )

        liked_record, disliked_record = (item for item, _ in catalog.saved)
        assert (liked_record.kind, liked_record.code) == ("liked", "USER_LIKED_ANSWER")
        assert (disliked_record.kind, disliked_record.code) == (
            "disliked",
            "USER_DISLIKED_ANSWER",
        )


class TestRequestContract:
    def test_a_dislike_without_a_reason_is_refused_at_the_edge(self) -> None:
        """裸的一个「踩」建模者拿到也不知道改什么，所以在入口就挡住。"""

        from knowflow_analytics.api import AnswerFeedbackRequest

        base = dict(
            question="各门店销售额是多少",
            release_id="rel-1",
            spec_hash="sha256:spec",
            index_snapshot_id="idx-1",
        )
        with pytest.raises(ValidationError):
            AnswerFeedbackRequest(liked=False, reason="", **base)

        assert AnswerFeedbackRequest(liked=True, **base).reason == ""
        assert AnswerFeedbackRequest(liked=False, reason="value", **base).reason == "value"


class TestPublishReminderExcludesLikes:
    """发布页那条提醒说的是「待处理的问数反馈」，赞不是缺口。

    过滤必须发生在聚合之前：`total` 是 SQL 数出来的真实种数，前端筛只会让
    数字和列表对不上（这正是当初把聚合下沉到 SQL 的原因）。
    """

    def test_exclude_kinds_reaches_the_store_before_aggregation(self) -> None:
        from knowflow_analytics.application import AnalyticsApplication

        class _Groups:
            def __init__(self) -> None:
                self.kwargs: dict = {}

            def list_failure_groups(self, **kwargs):
                self.kwargs = kwargs
                return (), 0

        catalog = _Groups()
        application = AnalyticsApplication.__new__(AnalyticsApplication)
        application.catalog = catalog  # type: ignore[attr-defined]

        AnalyticsApplication.list_query_failures(application, "prj-1", exclude_kinds=("liked",))

        assert catalog.kwargs["exclude_kinds"] == ("liked",)

    def test_the_feedback_page_still_sees_everything(self) -> None:
        from knowflow_analytics.application import AnalyticsApplication

        class _Groups:
            def __init__(self) -> None:
                self.kwargs: dict = {}

            def list_failure_groups(self, **kwargs):
                self.kwargs = kwargs
                return (), 0

        catalog = _Groups()
        application = AnalyticsApplication.__new__(AnalyticsApplication)
        application.catalog = catalog  # type: ignore[attr-defined]

        AnalyticsApplication.list_query_failures(application, "prj-1")

        assert catalog.kwargs["exclude_kinds"] == ()


class TestSelfContainedQuestion:
    """列表上的那句话必须是**这一轮真正问的**。

    下钻和追问的原话单独拎出来没有意义：「时间范围改为「近 90 天」」建模者读不懂，
    拿去试问也跑不出同一件事。改写后的完整问题只在核心自己的诊断产物里。
    """

    class _Artifact:
        def __init__(self, effective: str) -> None:
            from knowflow_analytics.query.contracts import QueryStage, QueryTraceStep

            self.trace = (
                QueryTraceStep(stage=QueryStage.PRECHECK, status="completed"),
                QueryTraceStep(
                    stage=QueryStage.FINAL_PARSING,
                    status="completed",
                    detail={"effective_question": effective},
                ),
            )

    def _application(self, artifact=None, error: Exception | None = None):
        from knowflow_analytics.application import AnalyticsApplication

        class _WithDiagnostic(_Catalog):
            def get_query_diagnostic(self, **_kwargs):
                if error is not None:
                    raise error
                return artifact

        application = AnalyticsApplication.__new__(AnalyticsApplication)
        catalog = _WithDiagnostic()
        application.catalog = catalog  # type: ignore[attr-defined]
        return application, catalog

    def _record_with(self, application, **overrides):
        from knowflow_analytics.application import AnalyticsApplication

        payload = dict(
            project_id="prj-1",
            actor_id="user-1",
            question="时间范围改为「近 90 天」",
            liked=True,
            release_id="rel-1",
            spec_hash="sha256:spec",
            index_snapshot_id="idx-1",
            query_id="q-1",
            permission_scope_hash="scope-1",
        )
        payload.update(overrides)
        AnalyticsApplication.record_answer_feedback(application, **payload)

    def test_the_rewritten_question_is_stored_alongside_the_raw_one(self) -> None:
        application, catalog = self._application(
            artifact=self._Artifact("各门店近 90 天的销售额是多少")
        )
        self._record_with(application)

        record, _ = catalog.saved[0]
        # 用户的原话留着，列表读的是自足的那句。
        assert record.question == "时间范围改为「近 90 天」"
        assert record.effective_question == "各门店近 90 天的销售额是多少"

    def test_an_expired_artifact_falls_back_to_the_raw_question(self) -> None:
        application, catalog = self._application(error=RuntimeError("expired"))
        self._record_with(application)

        record, _ = catalog.saved[0]
        assert record.effective_question == ""
        assert record.question == "时间范围改为「近 90 天」"

    def test_no_query_id_means_no_lookup_at_all(self) -> None:
        application, catalog = self._application(error=AssertionError("不该查诊断产物"))
        self._record_with(application, query_id="")

        assert catalog.saved[0][0].effective_question == ""
