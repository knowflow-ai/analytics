"""发布前试问不该每次都重建语义索引。

现场（2026-09-17）：自然语言试问「每次都失败，没成功过」。RAGFlow 侧网关日志显示，
一小时内四次试问，每次都先来一发 **16.5 MB / 18–26 秒**的 embedding——那是整份目录
在重新建索引——然后才是 30–60 秒的 S2SQL。两段加起来必然越过调用方 120 秒的请求超时，
用户看到的是转圈转到 `ANALYTICS_REQUEST_TIMEOUT`。

根因：``_stage_revision`` 只有在澄清续跑（调用方传了 ``index_snapshot_id``）时才去取
现成索引，普通试问一律重建；而且建完不存，所以下一次还得再建一遍。

索引的身份本来就是内容寻址的（2026-08-30：``release_spec_hash + embedding 模型 +
维度 + entries``），草稿没改就该复用。草稿一改 spec_hash 就变，自然重建一次——那是对的。
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.contracts import ModelingRevision, RevisionState
from knowflow_analytics.semantic.index import EmbeddingBatch


class _CountingEmbeddingGateway:
    """每次 encode 都记一笔——建索引会把整份目录的说法都发过来。"""

    def __init__(self) -> None:
        self.batches: list[int] = []

    def for_tenant(self, _tenant_id: str) -> _CountingEmbeddingGateway:
        return self

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        self.batches.append(len(texts))
        return EmbeddingBatch(
            model_id="index-reuse-test",
            dimension=2,
            vectors=tuple((1.0, 0.0) for _ in texts),
        )


@pytest.fixture()
def app(sales_catalog):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售")
    semantic_catalog = sales_catalog.model_copy(update={"revision_id": "revision-index"})
    revision = ModelingRevision(
        id="revision-index",
        project_id="sales",
        schema_snapshot_hash="sha256:schema",
        etag=3,
        state=RevisionState.VALIDATED,
        semantic_catalog=semantic_catalog,
        semantic_spec=compile_semantic_catalog(semantic_catalog),
    )
    catalog.save_revision(revision)
    gateway = _CountingEmbeddingGateway()
    application = AnalyticsApplication(
        catalog=catalog,
        introspector=object(),
        executor=Mock(),
        embedding_gateway=gateway,
    )
    try:
        yield application, revision, gateway
    finally:
        application.close()
        engine.dispose()


def test_a_second_preview_on_the_same_draft_reuses_the_index(app) -> None:
    """草稿没动，第二次试问不该再把整份目录嵌入一遍。"""

    application, revision, gateway = app

    application._stage_revision(revision, tenant_id="tenant-1")
    after_first = len(gateway.batches)
    application._stage_revision(revision, tenant_id="tenant-1")

    assert after_first >= 1, "第一次必须真的建一次"
    assert len(gateway.batches) == after_first, "第二次应当复用，一次 encode 都不该发"


def test_editing_the_draft_rebuilds_it(app) -> None:
    """改了目录就该重建：索引绑的是内容，不是版本号。"""

    application, revision, gateway = app
    application._stage_revision(revision, tenant_id="tenant-1")
    after_first = len(gateway.batches)

    edited_spec = revision.semantic_spec.model_copy(
        update={
            "spec_hash": revision.semantic_spec.spec_hash + "-edited",
            "metrics": tuple(
                item.model_copy(update={"aliases": ("新说法",)})
                for item in revision.semantic_spec.metrics
            ),
        }
    )
    edited = revision.model_copy(update={"semantic_spec": edited_spec, "etag": 4})

    application._stage_revision(edited, tenant_id="tenant-1")

    assert len(gateway.batches) > after_first


def test_the_reused_index_is_the_one_the_release_points_at(app) -> None:
    """复用不能只是"少发一次请求"：staged release 必须指向那个真实存在的快照。"""

    application, revision, _gateway = app

    first = application._stage_revision(revision, tenant_id="tenant-1")
    second = application._stage_revision(revision, tenant_id="tenant-1")

    assert second.index_snapshot.id == first.index_snapshot.id
    assert second.release.index_snapshot_id == first.index_snapshot.id
    stored = application.catalog.get_index_snapshot(first.index_snapshot.id, project_id="sales")
    assert stored.release_spec_hash == revision.semantic_spec.spec_hash
