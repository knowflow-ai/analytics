from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.catalog.store import CatalogError, CatalogStore
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.domain import DomainLifecycle
from knowflow_analytics.modeling.layout import (
    GraphNodePosition,
    GraphViewport,
    ModelGraphLayout,
    normalize_model_graph_layout,
)


def test_domain_governance_has_explicit_lifecycle_and_optimistic_updates():
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    store.create_project(project_id="sales", name="销售分析")

    initial = store.get_domain_governance("sales")
    assert initial.lifecycle is DomainLifecycle.INITIALIZED
    assert initial.classifications == ()

    updated = store.update_domain_governance(
        project_id="sales",
        expected_etag=initial.etag,
        classifications=("经营分析", "销售"),
        lifecycle=DomainLifecycle.OFFLINE,
        updated_by="owner",
    )

    assert updated.classifications == ("经营分析", "销售")
    assert updated.lifecycle is DomainLifecycle.OFFLINE
    assert updated.etag == initial.etag + 1
    with pytest.raises(CatalogError, match="changed"):
        store.update_domain_governance(
            project_id="sales",
            expected_etag=initial.etag,
            classifications=(),
            lifecycle=DomainLifecycle.INITIALIZED,
            updated_by="owner",
        )


def test_model_graph_layout_is_visual_only_and_rejects_unknown_nodes():
    layout = ModelGraphLayout(
        project_id="sales",
        revision_id="rev_1",
        etag=0,
        positions=(
            GraphNodePosition(model_id="orders", x=120, y=80),
            GraphNodePosition(model_id="customers", x=520, y=80),
        ),
        viewport=GraphViewport(x=10, y=20, zoom=0.8),
        updated_at=datetime(2026, 8, 17, tzinfo=UTC),
    )

    normalized = normalize_model_graph_layout(
        layout=layout,
        model_ids=("customers", "orders"),
    )
    assert {item.model_id for item in normalized.positions} == {"orders", "customers"}
    assert normalized.viewport.zoom == 0.8

    with pytest.raises(SemanticValidationError) as exc_info:
        normalize_model_graph_layout(layout=layout, model_ids=("orders", "products"))
    assert exc_info.value.code == "MODEL_GRAPH_LAYOUT_NODE_MISMATCH"


def test_model_graph_layout_store_uses_etag_and_does_not_touch_revision():
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    store.create_project(project_id="sales", name="销售分析")
    layout = ModelGraphLayout(
        project_id="sales",
        revision_id="rev_1",
        etag=0,
        positions=(GraphNodePosition(model_id="orders", x=0, y=0),),
        updated_at=datetime(2026, 8, 17, tzinfo=UTC),
    )

    saved = store.save_model_graph_layout(layout, expected_etag=0)
    assert saved.etag == 1
    assert store.get_model_graph_layout(project_id="sales", revision_id="rev_1") == saved

    moved = saved.model_copy(
        update={
            "positions": (GraphNodePosition(model_id="orders", x=300, y=200),),
            "updated_at": datetime(2026, 8, 17, 1, tzinfo=UTC),
        }
    )
    saved_again = store.save_model_graph_layout(moved, expected_etag=saved.etag)
    assert saved_again.etag == 2
    with pytest.raises(CatalogError, match="changed"):
        store.save_model_graph_layout(moved, expected_etag=saved.etag)


def test_get_projects_stored_layout_without_inventing_coordinates():
    """GET 只投影已存坐标,不给没有坐标的模型编造位置。

    此前 GET 会为每个缺坐标的模型合成 (index%3)*340,(index//3)*230 的占位格。
    两个后果:①前端"有模型没坐标就自动整理"的判断恒为假,自动整理成了死代码;
    ②占位格行距 230px,而节点高 = 74+22*字段数+30,字段数 ≥6 就压到下一行。
    导入新表后第一次打开画布因此经常重叠(2026-08-26 实测 5/9 个项目)。
    """

    from knowflow_analytics.modeling.layout import project_stored_layout

    stored = ModelGraphLayout(
        project_id="sales",
        revision_id="rev_1",
        etag=7,
        positions=(
            GraphNodePosition(model_id="orders", x=756, y=67),
            GraphNodePosition(model_id="dropped", x=1, y=2),
        ),
        viewport=GraphViewport(x=10, y=20, zoom=1.5),
        updated_by="owner",
        updated_at=datetime.now(UTC),
    )

    projected = project_stored_layout(
        stored=stored,
        model_ids=("orders", "customers"),
        project_id="sales",
        revision_id="rev_1",
    )

    # 新导入的 customers 没有坐标就是没有坐标,由前端排布后写回。
    assert [item.model_id for item in projected.positions] == ["orders"]
    assert projected.positions[0].x == 756
    # 已存的元信息原样保留,前端才能用正确的 etag 保存。
    assert projected.etag == 7
    assert projected.viewport.zoom == 1.5
    assert projected.updated_by == "owner"


def test_get_layout_with_nothing_stored_returns_no_positions():
    from knowflow_analytics.modeling.layout import project_stored_layout

    projected = project_stored_layout(
        stored=None,
        model_ids=("orders", "customers"),
        project_id="sales",
        revision_id="rev_1",
    )

    assert projected.positions == ()
    assert projected.etag == 0
    assert projected.updated_by is None
