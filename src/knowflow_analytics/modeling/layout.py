from __future__ import annotations

import math
from datetime import UTC, datetime

from pydantic import Field, model_validator

from knowflow_analytics.contracts import FrozenModel
from knowflow_analytics.errors import SemanticValidationError


class GraphNodePosition(FrozenModel):
    model_id: str = Field(min_length=1, max_length=128)
    x: float = Field(ge=-1_000_000, le=1_000_000)
    y: float = Field(ge=-1_000_000, le=1_000_000)

    @model_validator(mode="after")
    def finite_coordinates(self) -> GraphNodePosition:
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise ValueError("graph coordinates must be finite")
        return self


class GraphViewport(FrozenModel):
    x: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    y: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    zoom: float = Field(default=1, ge=0.1, le=4)


class ModelGraphLayout(FrozenModel):
    project_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    etag: int = Field(ge=0)
    positions: tuple[GraphNodePosition, ...] = Field(default=(), max_length=1_000)
    viewport: GraphViewport = Field(default_factory=GraphViewport)
    updated_by: str | None = Field(default=None, min_length=1, max_length=128)
    updated_at: datetime

    @model_validator(mode="after")
    def positions_are_unique(self) -> ModelGraphLayout:
        ids = [item.model_id for item in self.positions]
        if len(ids) != len(set(ids)):
            raise ValueError("graph positions must have unique model IDs")
        return self


def normalize_model_graph_layout(
    *,
    layout: ModelGraphLayout,
    model_ids: tuple[str, ...],
) -> ModelGraphLayout:
    """Validate a Canvas-like visual resource independently from ModelRela.

    View config is persisted separately from semantic relations, and it is
    typed rather than opaque so unknown nodes cannot be smuggled into the
    governed project view.
    """

    expected = set(model_ids)
    actual = {item.model_id for item in layout.positions}
    if actual != expected:
        raise SemanticValidationError(
            "model graph layout must contain exactly the current revision models",
            code="MODEL_GRAPH_LAYOUT_NODE_MISMATCH",
        )
    by_id = {item.model_id: item for item in layout.positions}
    return layout.model_copy(update={"positions": tuple(by_id[item] for item in sorted(expected))})


def project_stored_layout(
    *,
    stored: ModelGraphLayout | None,
    model_ids: tuple[str, ...],
    project_id: str,
    revision_id: str,
) -> ModelGraphLayout:
    """把已存布局投影到当前模型集合——不给缺坐标的模型编造位置。

    读取路径故意与 ``normalize_model_graph_layout`` 不同:写入必须恰好覆盖当前
    模型(拒绝把未知节点塞进受治理视图),读取只回答"已经排好的在哪里"。
    编造占位坐标会同时毁掉两件事:前端"有模型没坐标就自动整理"的判断恒为真,
    自动整理变成死代码;而占位格本身按固定行距铺开,节点高度随字段数变化,
    字段一多就相互压盖。新表由前端排布后写回,这里保持沉默。
    """

    known = set(model_ids)
    positions = tuple(
        sorted(
            (
                item
                for item in (stored.positions if stored is not None else ())
                if item.model_id in known
            ),
            key=lambda item: item.model_id,
        )
    )
    return ModelGraphLayout(
        project_id=project_id,
        revision_id=revision_id,
        etag=stored.etag if stored is not None else 0,
        positions=positions,
        viewport=stored.viewport if stored is not None else GraphViewport(),
        updated_by=stored.updated_by if stored is not None else None,
        updated_at=stored.updated_at if stored is not None else datetime.now(UTC),
    )
