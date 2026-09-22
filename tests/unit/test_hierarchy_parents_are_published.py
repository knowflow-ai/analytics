"""层级的父子配对要在发布期收进 release，运行时才不用读客户库。

`HierarchySpec` 只声明了层级**用哪两列**（上级列 → 主键列），树本身没有地方存：
`dimension_values` 是一维取值表，互不配对。于是问数期拿不到"应收账款有哪些下级"，
按父科目精确筛返回 0 行——而明细通常只记在末级上，界面把它渲染成"没有返回数据"，
用户读到的是一句关于自己业务的假话（实机 D008）。

这里钉住的是那条最容易被后人"优化"掉的判据：**只有能唯一标识节点的维度才配有
"上级取值"**。层级声明在模型上，但同模型的普通属性列套上去只会得到连接的副产物。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.contracts import (
    DimensionSpec,
    FieldKind,
    FieldSpec,
    HierarchySpec,
    ModelSpec,
    SemanticRelease,
)
from knowflow_analytics.execution.dialect import SqlDialect
from knowflow_analytics.modeling.profiler import DimensionValueProfiler


class _Connection:
    """只回放预置的父子行；真库连接在这条判据上没有信息量。"""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def begin(self):
        return self

    def exec_driver_sql(self, _statement):
        return None

    def execute(self, _query, _parameters=None):
        return self

    def all(self):
        return self._rows


class _Engine:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _Connection(self._rows)


def _dimension(dimension_id: str, name: str, column: str, dtype: str):
    field = FieldSpec(
        id=f"field:{column}",
        model_id="model:accounts",
        name=name,
        column=column,
        data_type=dtype,
        kind=FieldKind.DIMENSION,
    )
    dimension = DimensionSpec(
        id=dimension_id,
        name=name,
        model_id="model:accounts",
        field_id=field.id,
        semantic_type="categorical",
    )
    return field, dimension


@pytest.fixture
def accounts_release() -> SemanticRelease:
    code_field, code_dim = _dimension("dim:code", "科目编码", "account_code", "VARCHAR(20)")
    parent_field, parent_dim = _dimension(
        "dim:parent", "上级科目编码", "parent_code", "VARCHAR(20)"
    )
    name_field, name_dim = _dimension("dim:name", "科目名称", "account_name", "VARCHAR(50)")
    leaf_field, leaf_dim = _dimension("dim:leaf", "是否末级科目", "is_leaf", "INTEGER")
    model = ModelSpec(
        id="model:accounts",
        name="会计科目",
        schema_name="main",
        table="dim_account",
        query_type="table_query",
    )
    return SemanticRelease(
        id="rel_test",
        project_id="prj_test",
        spec_hash="sha256:test",
        models=(model,),
        fields=(code_field, parent_field, name_field, leaf_field),
        dimensions=(code_dim, parent_dim, name_dim, leaf_dim),
        hierarchies=(
            HierarchySpec(
                id="hier:accounts",
                model_id=model.id,
                name="父级科目编码",
                levels=(parent_dim.id, code_dim.id),
            ),
        ),
        datasets=(),
    )


def _resolve(rows, release, dimension_id):
    profiler = DimensionValueProfiler(_Engine(rows), dialect=SqlDialect.MYSQL)
    return profiler.resolve_hierarchy_parents(
        semantic_spec=release, dimension_ids=(dimension_id,)
    )


class TestTheTreeIsReadAtPublishTime:
    def test_a_node_naming_dimension_gets_its_parents(self, accounts_release):
        rows = [
            ("应收账款-甲公司", "应收账款"),
            ("应收账款-乙公司", "应收账款"),
            ("销售费用-差旅费", "销售费用"),
        ]

        parents = _resolve(rows, accounts_release, "dim:name")

        assert parents["dim:name"] == {
            "应收账款-甲公司": "应收账款",
            "应收账款-乙公司": "应收账款",
            "销售费用-差旅费": "销售费用",
        }


class TestOnlyNodeIdentifiersGetParents:
    def test_a_boolean_attribute_never_gets_a_parent(self, accounts_release):
        """子行 `is_leaf=1`、父行 `is_leaf=0`，每一行都产出「1 的上级是 0」。

        映射完全一致，却毫无意义——照它展开会让 `是否末级科目=0` 把 1 也算进去，
        比不展开更糟。所以判据不能是"映射一不一致"，必须是"这个取值能不能唯一
        标识一行"：行数多于去重后的取值数，就说明它不是节点标识。
        """

        parents = _resolve([(1, 0), (1, 0), (1, 0)], accounts_release, "dim:leaf")

        assert "dim:leaf" not in parents

    def test_a_shared_foreign_key_never_gets_a_parent(self, accounts_release):
        """`上级科目编码` 被多个子科目共享，同样不是节点标识。"""

        rows = [("1122", "1122"), ("1122", "1122"), ("6601", "6601")]

        parents = _resolve(rows, accounts_release, "dim:parent")

        assert "dim:parent" not in parents

    def test_one_clean_pair_still_counts(self, accounts_release):
        """别把判据写成"必须有多少对"——一棵只有一条边的树也是树。"""

        parents = _resolve([("应收账款-甲公司", "应收账款")], accounts_release, "dim:name")

        assert parents["dim:name"] == {"应收账款-甲公司": "应收账款"}
