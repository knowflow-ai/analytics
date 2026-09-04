"""助手级配置：填了就生效，没填跟随全局。

这套东西只有两个承诺，两个都必须钉住：**空值不改变任何现有部署的行为**，以及
**填了的值真的作用到那一次请求上**。第二条只有对着真实读点验才算数——参数一路传下来
却在某一层被忽略，是不会报错的。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.query.contracts import QueryOptions, QueryRequest


class TestBlankMeansFollowTheDeployment:
    def test_a_fresh_request_overrides_nothing(self) -> None:
        request = QueryRequest(project_id="p", question="各门店的销售额")

        assert request.options.model_dump(exclude_none=True) == {}

    @pytest.mark.parametrize(
        ("field", "fallback"),
        [
            pytest.param("self_consistency_number", 3, id="投票次数"),
            pytest.param("s2sql_corrector_enabled", True, id="S2SQL 校正"),
            pytest.param("dry_run_before_execute", True, id="执行前 dry-run"),
        ],
    )
    def test_an_unset_field_keeps_the_global_value(self, field: str, fallback: object) -> None:
        assert QueryOptions().merged(field, fallback) == fallback

    def test_false_is_a_choice_not_an_absence(self) -> None:
        """助手明确关掉某项时，不能被"没填"吞掉回落成全局的 True。

        用 ``or`` 写这段就会踩这个坑：False or True == True。
        """

        assert QueryOptions(s2sql_corrector_enabled=False).merged(
            "s2sql_corrector_enabled", True
        ) is False

    def test_one_vote_is_a_choice_too(self) -> None:
        """把投票次数明确设成 1（关闭）不能回落成全局的 5。"""

        assert QueryOptions(self_consistency_number=1).merged("self_consistency_number", 5) == 1


class TestBoundsAreEnforcedAtTheEdge:
    """越界值在构造时就拒绝，不要带着走到模型网关那一层才炸。"""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"self_consistency_number": 0}, id="投票次数不能为 0"),
            pytest.param({"self_consistency_number": 99}, id="投票次数上限"),
            pytest.param({"temperature": 1.5}, id="温度上限"),
            pytest.param({"temperature": -0.1}, id="温度下限"),
            pytest.param({"max_tokens": 0}, id="max_tokens 下限"),
            pytest.param({"max_tokens": 99_999}, id="max_tokens 上限"),
        ],
    )
    def test_an_out_of_range_value_is_refused(self, payload: dict) -> None:
        with pytest.raises(ValueError):
            QueryOptions(**payload)


class TestTheValueActuallyReachesTheReadSite:
    """传下来却在某一层被忽略，是不会报错的——只会让旋钮拧了没反应。"""

    def test_the_vote_count_drives_how_many_generations_happen(self, sales_release) -> None:
        from knowflow_analytics.query.contracts import MapMode, MappingResult
        from knowflow_analytics.query.parser import LlmS2SqlParser

        class _CountingGateway:
            def __init__(self) -> None:
                self.calls = 0

            def generate_json(self, **_kwargs):
                self.calls += 1
                return {"thought": "t", "sql": 'SELECT SUM("净收入") FROM "销售经营"'}

        def _parse(options: QueryOptions | None) -> int:
            gateway = _CountingGateway()
            # 装配期的全局默认是 1（单次生成）。
            parser = LlmS2SqlParser(gateway, self_consistency_number=1)
            parser.parse(
                question="净收入",
                release=sales_release,
                mapping=MappingResult(
                    dataset_id="sales_dataset",
                    mode=MapMode.STRICT,
                    normalized_question="净收入",
                    config_version="v1",
                    matches=(),
                ),
                query_id="q1",
                options=options,
            )
            return gateway.calls

        assert _parse(None) == 1, "没传 options 时应保持全局默认的单次生成"
        assert _parse(QueryOptions()) == 1, "空 options 等同于跟随全局"
        assert _parse(QueryOptions(self_consistency_number=3)) == 3, "助手设了 3 就该生成 3 次"

    def test_the_corrector_switch_reaches_the_corrector(self, sales_release) -> None:
        """全局关着、助手打开时，校正器必须真的跑起来。"""

        from knowflow_analytics.query.contracts import (
            MapMode,
            MappingResult,
            ParsedSemanticCandidate,
            SemanticQueryType,
        )
        from knowflow_analytics.query.corrector import LlmSqlCorrector

        class _Gateway:
            def __init__(self) -> None:
                self.calls = 0

            def generate_json(self, **_kwargs):
                self.calls += 1
                return {"thought": "t", "sql": 'SELECT SUM("净收入") FROM "销售经营"'}

        def _correct(options: QueryOptions | None) -> int:
            gateway = _Gateway()
            # 装配期全局关闭。
            corrector = LlmSqlCorrector(gateway, enabled=False)
            corrector.correct(
                candidate=ParsedSemanticCandidate(
                    id="c1",
                    dataset_id="sales_dataset",
                    parsed_s2sql='SELECT SUM("净收入") FROM "销售经营"',
                    corrected_s2sql='SELECT SUM("净收入") FROM "销售经营"',
                    query_type=SemanticQueryType.AGGREGATE,
                    score=1.0,
                    map_mode=MapMode.STRICT,
                    mapping=MappingResult(
                        dataset_id="sales_dataset",
                        mode=MapMode.STRICT,
                        normalized_question="净收入",
                        config_version="v1",
                        matches=(),
                    ),
                    parser="llm",
                ),
                question="净收入",
                release=sales_release,
                query_id="q1",
                options=options,
            )
            return gateway.calls

        assert _correct(None) == 0, "全局关着且没覆盖时不该调模型"
        assert _correct(QueryOptions()) == 0, "空 options 等同于跟随全局"
        assert _correct(QueryOptions(s2sql_corrector_enabled=True)) == 1, "助手打开就该跑"
