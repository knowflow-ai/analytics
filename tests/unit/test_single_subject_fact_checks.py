"""质量检查可以一次只核对一个对象。

发布前的整版报告是唯一入口时，建模者想确认一列是不是键，得先通过结构校验、再跑完
整张目录。拆出单对象方法之后，建模页可以就地核对，而批量报告仍旧在循环里调同一段
代码——两条路径给出的结论必须逐字相同，否则建模页说的和发布门说的会是两回事。
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog
from knowflow_analytics.modeling.quality import (
    MODEL_ROWS_PREVIEW_LIMIT,
    ModelingQualityError,
    ModelingQualityProfiler,
)

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"


@pytest.fixture
def release():
    return compile_semantic_catalog(
        SemanticCatalog.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))
    )


def _profiler() -> ModelingQualityProfiler:
    profiler = ModelingQualityProfiler(Mock(), Mock())
    profiler._model_source = lambda model, release, prefix="m_": ("SELECT 1", {})
    return profiler


class _Result:
    """够 _execute_rows 用的游标替身。"""

    def __init__(self, columns: tuple[str, ...], rows: tuple[tuple[Any, ...], ...]) -> None:
        self._columns = columns
        self._rows = rows

    def keys(self) -> tuple[str, ...]:
        return self._columns

    def fetchall(self) -> tuple[tuple[Any, ...], ...]:
        return self._rows


def test_one_model_gets_the_same_verdict_as_the_full_report(release) -> None:
    profiler = _profiler()
    profiler._execute_one = lambda query, parameters: (43697, 0, 1924)

    batch = profiler._profile_grains(release, started=time.monotonic())
    by_model = {item.model_id: item for item in batch}

    for model in release.models:
        assert profiler.profile_grain(model, release) == by_model[model.id]


def test_one_relation_gets_the_same_verdict_as_the_full_report(release) -> None:
    profiler = _profiler()
    profiler._execute_one = lambda query, parameters: (100, 20, 100, 20, 5, 1, 100)

    batch = profiler._profile_relations(release, started=time.monotonic())
    by_relation = {item.relation_id: item for item in batch}

    assert by_relation, "夹具里至少有一条关系"
    for relation in release.relations:
        measured = profiler.profile_relation(relation, release)
        expected = by_relation[relation.id]
        assert measured == expected


def test_row_preview_reads_the_governed_source_and_marks_more_rows(release) -> None:
    """预览必须走受治理来源：裸表能看到行级过滤挡掉的行，问数时却查不到。"""

    model = release.models[0]
    profiler = _profiler()
    seen: dict[str, str] = {}

    def run(query: Any, parameters: dict[str, Any], fetch: Any) -> Any:
        seen["sql"] = str(query)
        rows = tuple((index, Decimal("1.500")) for index in range(4))
        return fetch(_Result(("id", "amount"), rows))

    profiler._run = run

    preview = profiler.preview_model_rows(model, release, limit=3)

    assert "WITH source AS (SELECT 1)" in seen["sql"]
    # 多取一行只为判断还有没有更多，不返回给调用方。
    assert "LIMIT 4" in seen["sql"]
    assert preview.model_id == model.id
    assert preview.columns == ("id", "amount")
    assert len(preview.rows) == 3
    assert preview.truncated is True
    # 数值按执行器那套规则归一，尾零不进界面。
    assert str(preview.rows[0][1]) == "1.5"


def test_row_preview_refuses_to_become_an_export(release) -> None:
    profiler = _profiler()

    with pytest.raises(ModelingQualityError):
        profiler.preview_model_rows(release.models[0], release, limit=MODEL_ROWS_PREVIEW_LIMIT + 1)
