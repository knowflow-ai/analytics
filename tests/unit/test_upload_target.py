"""上传数据落到哪里。

落错地方不是功能问题是边界问题：上传目标必须成为一条数据源记录，问数才连得上；
而目录库里躺着加密的连接串、发布版本和查询历史。所以要钉住"换库不换实例"。
"""

from __future__ import annotations

from knowflow_analytics.ingest.uploads import UPLOAD_DATABASE, upload_database_url


class TestUploadsLandInTheirOwnDatabase:
    def test_the_instance_and_credentials_are_reused(self) -> None:
        url = upload_database_url(
            "postgresql+psycopg://someone:secret@db.internal:5432/analytics_catalog"
        )

        assert url == f"postgresql+psycopg://someone:secret@db.internal:5432/{UPLOAD_DATABASE}"

    def test_the_catalog_database_is_never_the_target(self) -> None:
        """指向目录库的数据源 DSN 会把元数据库整个纳入查询连接的可达范围。

        语义层只暴露建模过的表，但连接本身的面大得多——加密连接串、发布版本、
        查询历史都在那个库里。
        """

        url = upload_database_url(
            "postgresql+psycopg://u:p@127.0.0.1:5456/analytics_catalog?sslmode=require"
        )

        assert url.rsplit("/", 1)[-1] == UPLOAD_DATABASE
        assert "analytics_catalog" not in url
