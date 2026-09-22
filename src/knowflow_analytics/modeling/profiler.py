from __future__ import annotations

import math
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from knowflow_analytics.contracts import SemanticRelease
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.execution.dialect import SqlDialect, to_dialect_sql
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.contracts import (
    DimensionDataProfile,
    ProfiledValue,
    SchemaSnapshot,
    SemanticDataProfile,
)
from knowflow_analytics.modeling.source_query import compile_governed_model_source


class SemanticProfileError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "SEMANTIC_PROFILE_FAILED") -> None:
        super().__init__(message, code=code, stage="MODELING_PROFILING")


class DimensionValueProfiler:
    """Read governed categorical evidence for a reviewed semantic specification.

    The value fetch groups a selected dimension over the full configured data
    range, orders its values by frequency, and limits the returned value set.
    Keep that exact evidence meaning: limiting source rows would change both the
    frequencies and which values are found at all.
    Read-only and statement timeouts bound execution without silently sampling.

    """

    def __init__(
        self,
        engine: Engine,
        *,
        max_values_per_dimension: int = 50,
        max_dimensions: int = 100,
        statement_timeout_ms: int = 3_000,
        overall_timeout_ms: int = 30_000,
        max_value_length: int = 256,
        dialect: SqlDialect = SqlDialect.POSTGRES,
    ) -> None:
        self._dialect = dialect
        if not 1 <= max_values_per_dimension <= 500:
            raise ValueError("max_values_per_dimension must be between 1 and 500")
        if not 1 <= max_dimensions <= 1_000:
            raise ValueError("max_dimensions must be between 1 and 1000")
        if not 100 <= statement_timeout_ms <= 60_000:
            raise ValueError("statement_timeout_ms must be between 100 and 60000")
        if not 1_000 <= overall_timeout_ms <= 300_000:
            raise ValueError("overall_timeout_ms must be between 1000 and 300000")
        if not 1 <= max_value_length <= 4_000:
            raise ValueError("max_value_length must be between 1 and 4000")
        self._engine = engine
        self._max_values = max_values_per_dimension
        self._max_dimensions = max_dimensions
        self._statement_timeout_ms = statement_timeout_ms
        self._overall_timeout_ms = overall_timeout_ms
        self._max_value_length = max_value_length

    def profile(
        self,
        *,
        snapshot: SchemaSnapshot,
        semantic_spec: SemanticRelease,
        dimension_ids: tuple[str, ...] | None = None,
    ) -> SemanticDataProfile:
        table_index = {(item.schema_name, item.name): item for item in snapshot.tables}
        model_index = {item.id: item for item in semantic_spec.models}
        field_index = {item.id: item for item in semantic_spec.fields}
        all_dimensions = {item.id: item for item in semantic_spec.dimensions}
        if dimension_ids is not None:
            unknown = sorted(set(dimension_ids) - set(all_dimensions))
            if unknown:
                raise SemanticProfileError(
                    f"unknown dimensions cannot be profiled: {unknown[:5]}",
                    code="INVALID_PROFILE_TARGET",
                )
            non_categorical = sorted(
                dimension_id
                for dimension_id in dimension_ids
                if all_dimensions[dimension_id].semantic_type != "categorical"
            )
            if non_categorical:
                raise SemanticProfileError(
                    f"only categorical dimensions can have value dictionaries: "
                    f"{non_categorical[:5]}",
                    code="INVALID_PROFILE_TARGET",
                )
            selected_ids = set(dimension_ids)
        else:
            selected_ids = {
                item.id for item in semantic_spec.dimensions if item.semantic_type == "categorical"
            }
        dimensions = sorted(
            (item for item in semantic_spec.dimensions if item.id in selected_ids),
            key=lambda item: item.id,
        )
        warnings: list[str] = []
        started_at = time.monotonic()
        if len(dimensions) > self._max_dimensions:
            warnings.append(f"分类维度共 {len(dimensions)} 个，仅画像前 {self._max_dimensions} 个")
            dimensions = dimensions[: self._max_dimensions]

        profiles: list[DimensionDataProfile] = []
        for dimension in dimensions:
            if (time.monotonic() - started_at) * 1_000 >= self._overall_timeout_ms:
                warnings.append(f"画像总耗时达到 {self._overall_timeout_ms}ms，剩余维度未读取")
                break
            model = model_index.get(dimension.model_id)
            field = field_index.get(dimension.field_id)
            if model is None or field is None or field.model_id != dimension.model_id:
                raise SemanticProfileError(
                    f"dimension {dimension.id} is not bound to a valid physical field",
                    code="INVALID_PROFILE_TARGET",
                )
            if model.query_type == "table_query":
                table = table_index.get((model.schema_name, model.table))
                if table is None or field.column not in {item.name for item in table.columns}:
                    raise SemanticProfileError(
                        f"dimension {dimension.id} is outside the frozen schema snapshot",
                        code="PROFILE_SCHEMA_DRIFT",
                    )
            try:
                source_sql, source_parameters = compile_governed_model_source(
                    model,
                    semantic_spec,
                    parameter_prefix="profile_filter",
                )
                profile, profile_warnings = self._profile_dimension(
                    source_sql=source_sql,
                    source_parameters=source_parameters,
                    column_name=field.column,
                    dimension_id=dimension.id,
                    model_id=model.id,
                    field_id=field.id,
                )
            except SQLAlchemyError:
                warnings.append(f"维度“{dimension.name}”画像失败，未生成维度值候选")
                continue
            profiles.append(profile)
            warnings.extend(f"维度“{dimension.name}”：{item}" for item in profile_warnings)

        payload = {
            "schema_snapshot_hash": snapshot.content_hash,
            "dimensions": [item.model_dump(mode="json") for item in profiles],
            "warnings": warnings,
        }
        digest = content_hash(payload)
        return SemanticDataProfile(
            id=f"profile_{digest.removeprefix('sha256:')[:16]}",
            schema_snapshot_hash=snapshot.content_hash,
            content_hash=digest,
            captured_at=datetime.now(UTC),
            dimensions=tuple(profiles),
            warnings=tuple(warnings),
        )

    def resolve_hierarchy_parents(
        self,
        *,
        semantic_spec: SemanticRelease,
        dimension_ids: tuple[str, ...],
    ) -> dict[str, dict[object, object]]:
        """把已声明层级的父子配对读出来，按「取值 → 上级取值」返回。

        `HierarchySpec` 只声明了层级**用哪两列**（上级列 → 主键列），树本身没有
        地方存：`dimension_values` 是一维的取值表，互不配对。问数期因此拿不到
        "应收账款有哪些下级"，按父科目精确筛返回 0 行——而明细通常只记在末级上，
        界面把它渲染成"没有返回数据"，用户读到一句关于自己业务的假话。

        这里在**发布期**读一次，结果落进 `DimensionValueSpec.parent_value`，
        运行时就是确定性查表，不再碰客户库。

        只处理自引用层级：两级都在同一个模型上。跨模型的层级（省表→市表）是另一种
        形状，它的父子关系已经由 `RelationSpec` 表达，不需要也不应该塞进取值里。
        """

        models = {item.id: item for item in semantic_spec.models}
        fields = {item.id: item for item in semantic_spec.fields}
        dimensions = {item.id: item for item in semantic_spec.dimensions}
        wanted = set(dimension_ids)
        out: dict[str, dict[object, object]] = {}

        for hierarchy in semantic_spec.hierarchies:
            if len(hierarchy.levels) != 2:
                continue
            parent_dim, key_dim = (dimensions.get(i) for i in hierarchy.levels)
            model = models.get(hierarchy.model_id)
            if parent_dim is None or key_dim is None or model is None:
                continue
            parent_field, key_field = fields.get(parent_dim.field_id), fields.get(key_dim.field_id)
            if parent_field is None or key_field is None:
                continue
            # 自引用才有"同一维度里的上级取值"这回事
            if {parent_field.model_id, key_field.model_id} != {model.id}:
                continue

            for dimension_id in sorted(wanted):
                labelled = dimensions.get(dimension_id)
                if labelled is None or labelled.model_id != model.id:
                    continue
                label_field = fields.get(labelled.field_id)
                if label_field is None:
                    continue
                pairs = self._hierarchy_pairs(
                    semantic_spec=semantic_spec,
                    model=model,
                    label_column=label_field.column,
                    key_column=key_field.column,
                    parent_column=parent_field.column,
                )
                if pairs:
                    out.setdefault(dimension_id, {}).update(pairs)
        return out

    def _hierarchy_pairs(
        self,
        *,
        semantic_spec: SemanticRelease,
        model: object,
        label_column: str,
        key_column: str,
        parent_column: str,
    ) -> dict[object, object]:
        source_sql, source_parameters = compile_governed_model_source(
            model,
            semantic_spec,
            parameter_prefix="hierarchy_filter",
        )
        child, parent = _quote_identifier(label_column), _quote_identifier(label_column)
        key, up = _quote_identifier(key_column), _quote_identifier(parent_column)
        query = text(
            to_dialect_sql(
                f"""
            SELECT c.{child} AS child_value, p.{parent} AS parent_value
            FROM ({source_sql}) AS c
            JOIN ({source_sql}) AS p ON c.{up} = p.{key}
            WHERE c.{child} IS NOT NULL AND p.{parent} IS NOT NULL
              AND c.{child} <> p.{parent}
            LIMIT :pair_rows
            """,
                self._dialect,
            )
        )
        with self._engine.connect() as connection, connection.begin():
            for statement in self._dialect.read_only_session_sql(
                statement_timeout_ms=self._statement_timeout_ms, lock_timeout_ms=1_000
            ):
                connection.exec_driver_sql(statement)
            rows = connection.execute(
                query,
                {**source_parameters, "pair_rows": self._max_values + 1},
            ).all()
        # **只有能唯一标识节点的维度才有"上级取值"这回事。**
        # 层级声明在模型上，但同模型的其它列（`是否末级科目` 这种布尔属性）套上去
        # 会得到连接的副产物：子行 is_leaf=1、父行 is_leaf=0，于是产出「1 的上级是 0」。
        # 照它展开会让 `是否末级科目=0` 把 1 也算进去——比不展开更糟。
        #
        # 判据是数据给的，不靠字段命名：同一个取值映射到多个不同上级，就说明它不是
        # 节点标识，整个维度丢弃。实测会正确留下 科目名称 / 科目编码 / 科目ID，
        # 丢掉 是否末级科目 与 上级科目编码。
        pairs: dict[object, object] = {}
        for child_value, up_value in rows:
            pairs[_normalise_value(child_value)] = _normalise_value(up_value)
        # 判据是"这个取值能不能唯一标识一行"，不是"映射一不一致"。
        # `是否末级科目` 的每一行都是 (子 1, 父 0)，映射完全一致却毫无意义——
        # 行数多于去重后的取值数，说明一个取值对应很多行，它不是节点标识。
        # 实测正确留下 科目编码 / 科目ID / 科目名称，丢掉 是否末级科目 与 上级科目编码。
        if len(rows) != len(pairs):
            return {}
        return pairs

    def _profile_dimension(
        self,
        *,
        source_sql: str,
        source_parameters: dict[str, object],
        column_name: str,
        dimension_id: str,
        model_id: str,
        field_id: str,
    ) -> tuple[DimensionDataProfile, tuple[str, ...]]:
        column = _quote_identifier(column_name)
        # 按内部记法（PostgreSQL 写法）拼，交给方言层落地：``::bigint`` 与
        # ``CAST(... AS text)`` 在 MySQL 上都是语法错。
        query = text(
            to_dialect_sql(
                f"""
            WITH frequencies AS (
                SELECT {column} AS value, COUNT(*)::bigint AS frequency
                FROM ({source_sql}) AS governed_profile_source
                WHERE {column} IS NOT NULL
                GROUP BY {column}
            )
            SELECT
                value,
                frequency,
                SUM(frequency) OVER ()::bigint AS profiled_rows,
                COUNT(*) OVER ()::bigint AS observed_distinct_values
            FROM frequencies
            ORDER BY frequency DESC, CAST(value AS text)
            LIMIT :value_rows
            """,
                self._dialect,
            )
        )
        with self._engine.connect() as connection, connection.begin():
            for statement in self._dialect.read_only_session_sql(
                statement_timeout_ms=self._statement_timeout_ms, lock_timeout_ms=1_000
            ):
                connection.exec_driver_sql(statement)
            rows = connection.execute(
                query,
                {**source_parameters, "value_rows": self._max_values + 1},
            ).all()

        if not rows:
            return (
                DimensionDataProfile(
                    dimension_id=dimension_id,
                    model_id=model_id,
                    field_id=field_id,
                    sampled_rows=0,
                    observed_distinct_values=0,
                ),
                (),
            )

        sampled_rows = int(rows[0][2])
        observed_distinct_values = int(rows[0][3])
        truncated = observed_distinct_values > self._max_values
        warnings: list[str] = []
        values: list[ProfiledValue] = []
        for raw_value, frequency, *_rest in rows[: self._max_values]:
            value = _normalise_value(raw_value)
            if value is None:
                warnings.append("存在无法安全序列化的值，已跳过")
                continue
            if isinstance(value, str) and len(value) > self._max_value_length:
                warnings.append(
                    f"存在长度超过 {self._max_value_length} 的值，已跳过且不会进入语义索引"
                )
                continue
            values.append(ProfiledValue(value=value, frequency=int(frequency)))
        if truncated:
            warnings.append(
                f"完整数据范围内有 {observed_distinct_values} 个不同值，"
                f"仅保留频次最高的 {self._max_values} 个"
            )
        return (
            DimensionDataProfile(
                dimension_id=dimension_id,
                model_id=model_id,
                field_id=field_id,
                sampled_rows=sampled_rows,
                observed_distinct_values=observed_distinct_values,
                source_rows_truncated=False,
                truncated=truncated,
                values=tuple(values),
            ),
            tuple(dict.fromkeys(warnings)),
        )


def _quote_identifier(value: str) -> str:
    if "\x00" in value or len(value.encode("utf-8")) > 63:
        raise SemanticProfileError("invalid PostgreSQL identifier", code="INVALID_PROFILE_TARGET")
    return '"' + value.replace('"', '""') + '"'


def _normalise_value(value: object) -> str | int | float | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, UUID)):
        return value.isoformat() if isinstance(value, (datetime, date)) else str(value)
    if isinstance(value, str):
        return value
    return None
