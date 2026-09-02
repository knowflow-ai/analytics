"""行列级权限的**执行端**合同。

检索侧过滤（`test_column_level_visibility.py`）管的是"模型表达不出来"；这里管的
是"表达出来也执行不了"。两层都要有：Prompt 收窄依赖模型配合，翻译层才是确定性
边界。任何一条能绕开的路径——结构化 Playground、下钻、QueryRule、S2SQL 改写——
都在这里被同一个收口挡住。

行级权限注在数据源包装处而不是语义查询的过滤列表上，所以自连接 CTE（RATIO_*）、
UNION 分支、指标作用域子查询自动带上同一条谓词。这条不是推理，是本文件实测。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.contracts import FilterOperator, FixedFilter
from knowflow_analytics.errors import TranslationError
from knowflow_analytics.query.contracts import SemanticQuery, SemanticQueryType
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator
from knowflow_analytics.semantic.translator import SemanticTranslator

_REGION_EAST = FixedFilter(
    field_id="orders.region", operator=FilterOperator.EQ, value="华东"
)


def _query(**overrides) -> SemanticQuery:
    base = {
        "dataset_id": "sales_dataset",
        "query_type": SemanticQueryType.AGGREGATE,
        "metric_ids": ("net_revenue",),
        "dimension_ids": ("region",),
        "limit": 100,
    }
    return SemanticQuery(**{**base, **overrides})


class TestColumnEnforcement:
    def test_invisible_metric_cannot_be_executed(self, sales_release):
        """检索侧漏了、模型硬写了、下钻 token 旧了——都在这里止住。"""

        with pytest.raises(TranslationError) as failure:
            SemanticTranslator().translate(
                release=sales_release,
                query=_query(metric_ids=("refund_amount",)),
                visible_element_ids=frozenset({"net_revenue", "region"}),
            )

        assert failure.value.code == "UNKNOWN_METRIC"

    def test_invisible_member_is_indistinguishable_from_a_missing_one(self, sales_release):
        """"存在但你看不到"和"不存在"必须报同一个错。

        区分开来本身就是泄漏：不同的错误码等于确认了该指标存在。
        """

        with pytest.raises(TranslationError) as invisible:
            SemanticTranslator().translate(
                release=sales_release,
                query=_query(metric_ids=("refund_amount",)),
                visible_element_ids=frozenset({"net_revenue", "region"}),
            )
        with pytest.raises(TranslationError) as absent:
            SemanticTranslator().translate(
                release=sales_release,
                query=_query(metric_ids=("no_such_metric",)),
            )

        assert invisible.value.code == absent.value.code

    def test_invisible_dimension_cannot_be_grouped_by(self, sales_release):
        with pytest.raises(TranslationError) as failure:
            SemanticTranslator().translate(
                release=sales_release,
                query=_query(dimension_ids=("channel",)),
                visible_element_ids=frozenset({"net_revenue", "region"}),
            )

        assert failure.value.code == "UNKNOWN_DIMENSION"

    def test_visible_members_still_execute(self, sales_release):
        physical = SemanticTranslator().translate(
            release=sales_release,
            query=_query(),
            visible_element_ids=frozenset({"net_revenue", "region"}),
        )

        assert "net_amount" in physical.sql

    def test_none_means_unrestricted(self, sales_release):
        physical = SemanticTranslator().translate(
            release=sales_release, query=_query(metric_ids=("refund_amount",))
        )

        assert "refund_amount" in physical.sql

    def test_the_textual_s2sql_path_is_gated_too(self, sales_release):
        """自然语言路径是客户实际走的那条：护栏只装在结构化路径等于没装。"""

        with pytest.raises(TranslationError):
            S2SqlSemanticTranslator().translate(
                release=sales_release,
                dataset_id="sales_dataset",
                corrected_s2sql='SELECT SUM("退款金额") FROM "销售经营"',
                visible_element_ids=frozenset({"net_revenue", "region"}),
            )


class TestRowEnforcement:
    def test_row_filter_reaches_the_physical_sql(self, sales_release):
        physical = SemanticTranslator().translate(
            release=sales_release,
            query=_query(),
            row_filters={"orders": (_REGION_EAST,)},
        )

        assert "region" in physical.sql
        assert "华东" in physical.parameters.values()

    def test_row_filter_wraps_the_source_not_the_outer_where(self, sales_release):
        """包在数据源上，聚合与 JOIN 都发生在过滤之后。

        注在外层 WHERE 会让 JOIN 先扇出再过滤——行数对，但 LEFT JOIN 的空侧
        和窗口函数的分母都已经算错了。
        """

        physical = SemanticTranslator().translate(
            release=sales_release, query=_query(), row_filters={"orders": (_REGION_EAST,)}
        )

        assert '(SELECT * FROM "analytics_v0"."orders" WHERE ("region" = ' in physical.sql

    def test_row_filter_applies_to_a_joined_model(self, sales_release):
        """JOIN 进来的表也要过滤，否则从关联侧就能反推出被挡住的行。"""

        physical = SemanticTranslator().translate(
            release=sales_release,
            query=_query(dimension_ids=("customer_segment",)),
            row_filters={
                "customers": (
                    FixedFilter(
                        field_id="customers.segment",
                        operator=FilterOperator.EQ,
                        value="VIP",
                    ),
                )
            },
        )

        assert "VIP" in physical.parameters.values()

    def test_ratio_self_join_cte_carries_the_row_filter_on_both_sides(self, sales_release):
        """RATIO_* 的自连接 CTE 是最容易漏的一处：当期与对比期读的是同一个源。

        如果只在外层注入，对比期那一侧就是未过滤的全量数据，用户能从同比的
        分母反解出他无权看到的行的汇总值。
        """

        translated = S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT DATE_TRUNC(\'month\', "下单日期") AS "_月份_", '
                'RATIO_OVER("净收入") AS "_同比_" '
                'FROM "销售经营" GROUP BY DATE_TRUNC(\'month\', "下单日期")'
            ),
            row_filters={"orders": (_REGION_EAST,)},
        )

        sql = translated.physical_query.sql
        # 实测结构：源包装进 __kf_dataset，__kf_ratio_base 从它取数，当期与对比期
        # 都是 __kf_ratio_base 的别名。一次注入，自连接两侧都带上。
        assert '(SELECT * FROM "analytics_v0"."orders" WHERE ("region" = ' in sql
        assert '"__kf_ratio_base" AS "__kf_current"' in sql
        assert '"__kf_ratio_base" AS "__kf_previous"' in sql
        assert "华东" in translated.physical_query.parameters.values()

    def test_no_row_filter_leaves_the_source_untouched(self, sales_release):
        physical = SemanticTranslator().translate(release=sales_release, query=_query())

        assert "(SELECT * FROM" not in physical.sql

    def test_row_filters_and_model_filters_compose(self, sales_release):
        """建模期声明的模型过滤仍然生效，权限只叠加、不替换。"""

        models = tuple(
            item.model_copy(
                update={
                    "filters": (
                        FixedFilter(
                            field_id="orders.channel",
                            operator=FilterOperator.EQ,
                            value="线上",
                        ),
                    )
                }
            )
            if item.id == "orders"
            else item
            for item in sales_release.models
        )
        release = sales_release.model_copy(update={"models": models})

        physical = SemanticTranslator().translate(
            release=release, query=_query(), row_filters={"orders": (_REGION_EAST,)}
        )

        values = set(physical.parameters.values())
        assert {"线上", "华东"} <= values


class TestFailureIsActionable:
    def test_an_unresolvable_row_scope_dimension_says_what_to_fix(self):
        """拒绝要说对原因：这是管理员改配置的事，不是用户换说法的事。

        套用 FINAL_PARSING 的通用文案会变成「没能理解这个问题，请换一种说法」。
        用户换一百种说法也没用，而真正该动的人（管理员）根本不会看到这条。
        实机发现：拒绝本身是对的，文案把它伪装成了理解失败。
        """

        from knowflow_analytics.query.contracts import QueryStage
        from knowflow_analytics.query.errors import SemanticParsingError
        from knowflow_analytics.query.service import _error_diagnosis

        diagnosis = _error_diagnosis(
            SemanticParsingError(
                "数据范围里引用的维度在当前版本中不存在，请联系管理员更新该项目的数据范围配置。",
                code="ROW_SCOPE_DIMENSION_UNRESOLVED",
                stage=QueryStage.PRECHECK,
            )
        )

        assert diagnosis.stage == QueryStage.PRECHECK.value
        assert "换一种说法" not in diagnosis.user_hint
        assert "数据范围" in diagnosis.user_hint
        assert "管理员" in diagnosis.user_hint


def test_the_structured_integration_endpoint_carries_permissions():
    """「集成方入口」也必须带行列级权限，否则它就是一扇后门。

    `/v1/analytics/structured-query` 绕过 Mapper 与 LLM 直达 Translator。内部下钻
    走的是同一个 query_structured 且早已带上权限，唯独这个 HTTP 端点漏了——
    实施行列级权限当天没覆盖到，事后自查发现。
    """

    from fastapi.testclient import TestClient

    from knowflow_analytics.api import create_api

    secret = "s" * 32
    seen: dict[str, object] = {}

    class _Application:
        def structured_query(self, request, **_kwargs):
            seen["allowed"] = request.allowed_element_ids
            seen["filters"] = request.row_filters
            raise RuntimeError("stop after capturing the request")

    client = TestClient(
        create_api(application=_Application(), service_secret=secret),
        raise_server_exceptions=False,
    )
    client.post(
        "/v1/analytics/structured-query",
        headers={
            "X-KnowFlow-Service-Token": secret,
            "X-KnowFlow-Actor-Id": "actor-1",
            "X-KnowFlow-Project-Id": "sales",
            "X-KnowFlow-Permission-Scope-Hash": "scope-hash",
        },
        json={
            "project_id": "sales",
            "semantic_query": {
                "dataset_id": "ds",
                "query_type": "aggregate",
                "metric_ids": ["m1"],
                "dimension_ids": ["d1"],
                "limit": 100,
            },
            "allowed_element_ids": ["m1", "d1"],
            "row_filters": [{"dimension_id": "d1", "values": ["华东"]}],
        },
    )

    assert seen["allowed"] == ("m1", "d1")
    assert seen["filters"] and seen["filters"][0].dimension_id == "d1"
