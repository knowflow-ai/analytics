"""可导表的 schema 列表。

列错了不会报错，只会让用户在选择器里看到一堆没用的东西——空 schema 选进去一张表
都没有，系统 schema 里是引擎内部的上百张表。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.modeling.introspector import _is_system_schema


class TestSystemSchemas:
    @pytest.mark.parametrize(
        "name",
        ["information_schema", "pg_catalog", "pg_toast", "pg_temp_1", "pg_toast_temp_3"],
    )
    def test_postgres_system_schemas_are_hidden(self, name: str):
        # PostgreSQL 的系统 schema 数量不定（pg_temp_N 随会话生成），只能按前缀判。
        assert _is_system_schema(name)

    @pytest.mark.parametrize("name", ["mysql", "performance_schema", "sys"])
    def test_mysql_system_schemas_are_hidden(self, name: str):
        """MySQL 的系统库要排掉。

        原来的排除规则是 PostgreSQL 形状的（information_schema 与 pg_*），实测
        MySQL 上会把 mysql(38 张表)、performance_schema(111)、sys(101) 一起列给
        用户——那是引擎内部，不是业务库。
        """

        assert _is_system_schema(name)

    @pytest.mark.parametrize("name", ["INFORMATION_SCHEMA", "MySQL", "SYS"])
    def test_matching_ignores_case(self, name: str):
        # MySQL 在部分平台上大小写不敏感，元数据里可能是大写。
        assert _is_system_schema(name)

    @pytest.mark.parametrize(
        "name",
        ["public", "demo_cafe", "analytics_v0", "dwd", "pgdata", "system_of_record"],
    )
    def test_business_schemas_are_kept(self, name: str):
        """别误伤。

        ``pgdata`` 以 pg 开头但不是 ``pg_`` 前缀；``system_of_record`` 含 system
        但不是系统库。按前缀和全名判，不按包含判。
        """

        assert not _is_system_schema(name)
