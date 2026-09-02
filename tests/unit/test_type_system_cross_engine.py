"""列类型判定要同时认得 PostgreSQL 与 MySQL 的写法。

判错**不会报错**，只会让一列悄悄没被建模：MySQL 的 ``DOUBLE`` 判不成数值，金额列
就走不到度量；``DATETIME`` 判不成时间，时间维度和同比全没了。所以逐个类型钉住。

这些名字是 SQLAlchemy ``inspect()`` 从真库读回来的形式（实测），不是文档抄来的。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.modeling.type_system import (
    is_numeric_type,
    is_temporal_type,
    is_text_type,
)


def _kind(data_type: str) -> str:
    if is_numeric_type(data_type):
        return "num"
    if is_temporal_type(data_type):
        return "time"
    if is_text_type(data_type):
        return "text"
    return "-"


# 真 MySQL 8.0 上 inspect() 读回来的类型名。改造前 20 个里判错 8 个。
_MYSQL = {
    "TINYINT": "num",
    "SMALLINT": "num",
    "MEDIUMINT": "num",
    "INTEGER": "num",
    "BIGINT": "num",
    "DECIMAL(18, 2)": "num",
    "FLOAT": "num",
    "DOUBLE": "num",
    "DATE": "time",
    "DATETIME": "time",
    "TIMESTAMP": "time",
    "TIME": "time",
    "CHAR(4)": "text",
    "VARCHAR(64)": "text",
    "TEXT": "text",
    "LONGTEXT": "text",
    "MEDIUMTEXT": "text",
    "TINYTEXT": "text",
    "ENUM": "text",
    # YEAR 刻意不算时间：它只是个年份，按日/周/月截断没有意义。
    "YEAR": "-",
    "JSON": "-",
}

# 真 PostgreSQL 上 inspect() 读回来的类型名。这一组一个都不能变。
_POSTGRES = {
    "SMALLINT": "num",
    "INTEGER": "num",
    "BIGINT": "num",
    "DECIMAL(18, 2)": "num",
    "NUMERIC": "num",
    "REAL": "num",
    "DOUBLE PRECISION": "num",
    "MONEY": "num",
    "DATE": "time",
    "TIME": "time",
    "TIMESTAMP": "time",
    "TIMESTAMP WITH TIME ZONE": "time",
    "TEXT": "text",
    "VARCHAR(64)": "text",
    "CHAR(4)": "text",
    "VARCHAR(10)": "text",
    "INTERVAL": "-",
    "BOOLEAN": "-",
    "JSONB": "-",
    "UUID": "-",
}


@pytest.mark.parametrize(("data_type", "expected"), sorted(_MYSQL.items()))
def test_mysql_types_are_classified(data_type: str, expected: str):
    assert _kind(data_type) == expected


@pytest.mark.parametrize(("data_type", "expected"), sorted(_POSTGRES.items()))
def test_postgres_types_keep_their_classification(data_type: str, expected: str):
    assert _kind(data_type) == expected


class TestTheTrapsBehindThoseNames:
    def test_datetime_needs_its_own_alternative(self):
        """``DATETIME`` 不能靠 ``date`` 那一支匹配到。

        正则锚在行首，``date`` 后面跟着 ``t`` 过不了 ``\\b``，而且不会回溯去试
        ``time``——所以 MySQL 的 DATETIME 原来一个都匹配不上，整张表的时间维度
        直接消失。
        """

        assert is_temporal_type("DATETIME")

    def test_bare_double_counts_as_numeric(self):
        """MySQL 写 ``DOUBLE``，PostgreSQL 写 ``DOUBLE PRECISION``。

        只认后者的话，MySQL 上所有浮点金额列都不是数值，成不了度量。
        """

        assert is_numeric_type("DOUBLE")
        assert is_numeric_type("DOUBLE PRECISION")

    def test_year_is_not_a_time_dimension(self):
        """YEAR 归进时间类型会得到一个截不动的时间维度。

        按日/周/月截断一个年份没有意义。宁可不分类，让它走后面的规则。
        """

        assert not is_temporal_type("YEAR")

    def test_enum_is_categorical_text(self):
        # 取值受限的分类列，正是维度该有的样子。
        assert is_text_type("ENUM")

    def test_mysql_boolean_is_indistinguishable_from_tinyint(self):
        """MySQL 的 BOOLEAN 在元数据里就是 TINYINT。

        分辨不出来，所以一并当数值。这是 MySQL 自身的限制，不是判定的疏漏——
        写在测试里免得下次有人以为是 bug。
        """

        assert is_numeric_type("TINYINT")

    def test_postgres_boolean_is_still_not_numeric(self):
        # PostgreSQL 能区分，就别跟着 MySQL 一起退化。
        assert not is_numeric_type("BOOLEAN")
