"""S0 关系图：每张表的出入度、关联表与处理顺序。

两个用途：
1. 替换 prompt 里的 ``OtherRelatedDBSchema`` —— 此前每张表的调用都带上**其它所有表的
   完整列清单**，N 张表 = N 次调用各带 N 张表，payload 二次方。命名真正需要的上下文
   只有"和本表有外键关联的表叫什么、通过哪列连"。
2. 决定建模顺序：被引用多的维度表先，事实表最后，这样事实表的 ``customer_id`` 被命名
   时「客户」已经存在，跨表命名能累积。
"""

from __future__ import annotations

from pydantic import Field

from knowflow_analytics.contracts import FrozenModel
from knowflow_analytics.modeling.contracts import SchemaSnapshot, TableSnapshot

TableKey = tuple[str, str]


class RelatedTable(FrozenModel):
    schema_name: str
    table: str
    join_columns: tuple[tuple[str, str], ...] = Field(min_length=1)  # (local, remote)
    direction: str = Field(pattern="^(references|referenced_by)$")


class TableTopology(FrozenModel):
    schema_name: str
    table: str
    out_degree: int = Field(ge=0)
    in_degree: int = Field(ge=0)
    related: tuple[RelatedTable, ...] = ()
    order: int = Field(ge=0)


def build_topology(snapshot: SchemaSnapshot) -> dict[TableKey, TableTopology]:
    tables: dict[TableKey, TableSnapshot] = {
        (item.schema_name, item.name): item for item in snapshot.tables
    }
    related: dict[TableKey, list[RelatedTable]] = {key: [] for key in tables}
    in_degree: dict[TableKey, int] = {key: 0 for key in tables}

    for key, table in tables.items():
        for fk in table.foreign_keys:
            target = (fk.referred_schema, fk.referred_table)
            pairs = tuple(zip(fk.constrained_columns, fk.referred_columns, strict=False))
            related[key].append(
                RelatedTable(
                    schema_name=target[0],
                    table=target[1],
                    join_columns=pairs,
                    direction="references",
                )
            )
            if target in tables:
                in_degree[target] += 1
                related[target].append(
                    RelatedTable(
                        schema_name=key[0],
                        table=key[1],
                        join_columns=tuple((remote, local) for local, remote in pairs),
                        direction="referenced_by",
                    )
                )

    # 处理顺序：被引用越多越先（维度表），同分按出度少的先，再按名字稳定。
    ordered = sorted(
        tables,
        key=lambda key: (-in_degree[key], len(tables[key].foreign_keys), key),
    )
    order = {key: index for index, key in enumerate(ordered)}
    return {
        key: TableTopology(
            schema_name=key[0],
            table=key[1],
            out_degree=len(tables[key].foreign_keys),
            in_degree=in_degree[key],
            related=tuple(related[key]),
            order=order[key],
        )
        for key in tables
    }


def related_payload(topology: TableTopology) -> list[dict[str, object]]:
    """给 prompt 的关联表摘要：表名 + join 列，不带对方的列清单。"""

    return [
        {
            "table": f"{item.schema_name}.{item.table}",
            "direction": item.direction,
            "joinColumns": [
                {"local": local, "remote": remote} for local, remote in item.join_columns
            ],
        }
        for item in topology.related
    ]
