"""数据否决主标识：模型说是标识列，唯一率说不是键，数据说了算。

现场（2026-09-16）：`score_model_acct.zhhao` 411 个取值对 44224 行，唯一率 0.9%，
被标成主标识，一路到发布前才被质量报告拦下「0 条 NULL、41773 条重复记录」。

来路是盲判对账：规则判它是高基数文本维度，模型判它是标识列，不一致时默认采模型结论，
而且只要模型说是标识列就无条件写 `identifier_type="primary"`，不看画像里现成的唯一率。
一个错的主标识会静默重定义整张表的粒度，所以这里方向必须反过来。
"""

from __future__ import annotations

import json

import pytest

from knowflow_analytics.contracts import FieldKind, FieldSpec
from knowflow_analytics.modeling.classify import Prefill
from knowflow_analytics.modeling.contracts import SchemaColumnSnapshot, TableSnapshot
from knowflow_analytics.modeling.profile import ColumnProfile, TableProfile
from knowflow_analytics.modeling.workflow import NamingConventions, StagedTableModeler

_TABLE = "score_model_acct"
_COLUMN = "zhhao"
_ROWS = 44224
_DISTINCT = 411


class _ScriptedGateway:
    """命名照抄列名；盲判一律答「标识列」，复现模型那一票。"""

    def __init__(self, kind: str = "identifier") -> None:
        self._kind = kind

    def generate_json(self, *, purpose, messages, response_schema, trace):
        payload = json.loads(messages[-1]["content"])
        if purpose == "analytics.modeling.naming":
            return {
                "columns": [
                    {"column_name": item["column"], "name": item["column"], "description": ""}
                    for item in payload["columns"]
                ]
            }
        assert purpose == "analytics.modeling.classify", purpose
        return {
            "columns": [
                {
                    "index": item["index"],
                    "column_name": item["name"],
                    "kind": self._kind,
                    "reason": "看起来像账号",
                }
                for item in payload["columns"]
            ]
        }


def _snapshot() -> TableSnapshot:
    return TableSnapshot(
        schema_name="knowflow_analytics",
        name=_TABLE,
        columns=(
            SchemaColumnSnapshot(
                name=_COLUMN,
                data_type="varchar(64)",
                nullable=False,
                ordinal_position=0,
            ),
        ),
    )


def _profile(*, distinct: int | None, rows: int = _ROWS) -> TableProfile | None:
    if distinct is None:
        return None
    return TableProfile(
        schema_name="knowflow_analytics",
        table=_TABLE,
        row_count=rows,
        columns=(
            ColumnProfile(
                column=_COLUMN,
                row_count=rows,
                non_null_count=rows,
                distinct_count=distinct,
                sample_values=("6214...33fa", "6214...cd74"),
            ),
        ),
    )


def _build(profile: TableProfile | None) -> Prefill:
    modeler = StagedTableModeler(gateway=_ScriptedGateway())
    field = FieldSpec(
        id=f"{_TABLE}.{_COLUMN}",
        model_id=_TABLE,
        name=_COLUMN,
        column=_COLUMN,
        kind=FieldKind.DIMENSION,
    )
    # 规则第 13 条判它是高基数文本维度，属判断域，所以会被送去盲判。
    prefill = Prefill(
        column=_COLUMN,
        kind=FieldKind.DIMENSION,
        dimension_type="categorical",
        confidence=0.6,
        reason="高基数文本，可能是名称或自由文本",
        judgment_zone=True,
    )
    result = modeler.build_table(
        table=_snapshot(),
        fields=(field,),
        role=None,
        role_name="fact",
        topology=None,
        profile=profile,
        prefills={_COLUMN: prefill},
        conventions=NamingConventions(),
        trace={"revision_id": "rev_veto"},
    )
    return result.prefills[_COLUMN]


def test_data_vetoes_a_primary_identifier_the_model_asserted() -> None:
    """唯一率 0.9%：仍记为标识列，但绝不是主标识。

    选 foreign 而不是打回维度，是因为 foreign 是惰性的——粒度核对只读 primary，
    扇出判定读的是关系基数——而错的 primary 会静默重定义粒度。
    """

    prefill = _build(_profile(distinct=_DISTINCT))

    assert prefill.kind is FieldKind.IDENTIFIER
    assert prefill.identifier_type == "foreign"
    assert prefill.disputed is True
    assert "唯一率" in prefill.reason
    assert "411" in prefill.reason and "44224" in prefill.reason


def test_a_genuinely_unique_column_still_becomes_the_primary_identifier() -> None:
    prefill = _build(_profile(distinct=_ROWS))

    assert prefill.identifier_type == "primary"


@pytest.mark.parametrize(
    ("distinct", "rows"),
    [(None, _ROWS), (40, 40)],
    ids=["没有画像", "行数太少不足以判断"],
)
def test_without_evidence_no_primary_identifier_is_invented(
    distinct: int | None, rows: int
) -> None:
    """没有证据时不发明主标识：规则本来怎么说就怎么说，规则没说就记 foreign。"""

    prefill = _build(_profile(distinct=distinct, rows=rows))

    assert prefill.identifier_type == "foreign"
