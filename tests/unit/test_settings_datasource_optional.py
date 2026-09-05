"""默认业务库可以不配：注册表没有"默认库"概念，数据源在页面里逐个添加。"""

from __future__ import annotations

import pytest

from knowflow_analytics.settings import AnalyticsSettings

_REQUIRED = {
    "catalog_database_url": "postgresql+psycopg://a:b@127.0.0.1:5432/analytics_catalog",
    "service_secret": "s" * 32,
    "ragflow_base_url": "http://127.0.0.1:9380",
    "ragflow_service_token": "t" * 16,
}


def test_a_blank_default_datasource_is_allowed() -> None:
    """compose 用 `KEY=${VAR:-}` 传空串，不是缺省；空串必须与缺省同义。"""
    settings = AnalyticsSettings(**_REQUIRED, datasource_database_url="")
    assert settings.datasource_database_url.get_secret_value() == ""


def test_a_non_postgres_default_datasource_is_still_refused() -> None:
    with pytest.raises(ValueError, match="datasource_database_url must use PostgreSQL"):
        AnalyticsSettings(**_REQUIRED, datasource_database_url="mysql://a:b@h/d")
