from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from knowflow_analytics.contracts import (
    Cardinality,
    DatasetSpec,
    FieldKind,
    FieldSpec,
    JoinType,
    ModelSpec,
    SemanticRelease,
)
from knowflow_analytics.hashing import semantic_release_hash
from knowflow_analytics.modeling.classify import Prefill, classify_table, rule_based_role
from knowflow_analytics.modeling.contracts import (
    SchemaColumnSnapshot,
    SchemaSnapshot,
    SuggestionPatch,
    SuggestionSource,
)
from knowflow_analytics.modeling.profile import TableProfile
from knowflow_analytics.modeling.type_system import is_numeric_type

_DIMENSION_TYPES = ("char", "text", "bool", "enum", "uuid")


@dataclass(frozen=True)
class RuleModelingResult:
    semantic_spec: SemanticRelease
    suggestions: tuple[SuggestionPatch, ...]


class RuleSemanticModeller:
    """Create a lossless baseline and separate, reviewable classification suggestions."""

    def build(
        self,
        *,
        project_id: str,
        snapshot: SchemaSnapshot,
        create_default_dataset: bool = True,
        profiles: Mapping[tuple[str, str], TableProfile] | None = None,
    ) -> RuleModelingResult:
        """``profiles`` 有则走 S3 排除法分类（画像 + 表角色），没有则退回类型 + 列名。
        两者都不再把 ``year`` / ``status_code`` 标成可加度量。"""

        # 表角色兜底需要 FK 出入度：先扫一遍关系图。
        in_degree: dict[tuple[str, str], int] = {}
        out_degree: dict[tuple[str, str], int] = {}
        for table in snapshot.tables:
            key = (table.schema_name, table.name)
            out_degree[key] = len(table.foreign_keys)
            for foreign_key in table.foreign_keys:
                target = (foreign_key.referred_schema, foreign_key.referred_table)
                in_degree[target] = in_degree.get(target, 0) + 1
        models: list[ModelSpec] = []
        fields: list[FieldSpec] = []
        suggestions: list[SuggestionPatch] = []
        table_to_model: dict[tuple[str, str], str] = {}
        column_to_field: dict[tuple[str, str, str], str] = {}

        for table in snapshot.tables:
            model_id = _stable_id("model", table.schema_name, table.name)
            foreign_key_columns = {
                column
                for foreign_key in table.foreign_keys
                for column in foreign_key.constrained_columns
            }
            table_to_model[(table.schema_name, table.name)] = model_id
            models.append(
                ModelSpec(
                    id=model_id,
                    name=table.name,
                    table=table.name,
                    schema_name=table.schema_name,
                    description=table.comment,
                )
            )
            if table.comment:
                suggestions.append(
                    SuggestionPatch(
                        id=_stable_id("suggestion", model_id, "description"),
                        target_kind="model",
                        target_id=model_id,
                        changes={"name": table.comment, "description": table.comment},
                        source=SuggestionSource.RULE,
                        confidence=0.75,
                        reason="数据库表注释可作为初始业务名称与描述",
                    )
                )
            table_key = (table.schema_name, table.name)
            profile = profiles.get(table_key) if profiles else None
            numeric_non_key = sum(
                1
                for c in table.columns
                if is_numeric_type(c.data_type)
                and not c.primary_key
                and c.name not in foreign_key_columns
            )
            role = rule_based_role(
                table,
                in_degree=in_degree.get(table_key, 0),
                out_degree=out_degree.get(table_key, 0),
                prefills_numeric_non_key=numeric_non_key,
            )
            prefills = {
                item.column: item
                for item in classify_table(
                    table,
                    role=role,
                    profile=profile,
                    foreign_key_columns=frozenset(foreign_key_columns),
                )
            }
            for column in table.columns:
                field_id = _stable_id("field", table.schema_name, table.name, column.name)
                column_to_field[(table.schema_name, table.name, column.name)] = field_id
                fields.append(
                    FieldSpec(
                        id=field_id,
                        model_id=model_id,
                        name=column.name,
                        column=column.name,
                        data_type=column.data_type,
                        description=column.comment,
                        nullable=column.nullable,
                    )
                )
                suggestions.append(
                    self._field_suggestion(
                        model_id,
                        field_id,
                        column,
                        prefill=prefills[column.name],
                    )
                )

        for table in snapshot.tables:
            left_model_id = table_to_model[(table.schema_name, table.name)]
            column_index = {column.name: column for column in table.columns}
            for foreign_key in table.foreign_keys:
                right_model_id = table_to_model.get(
                    (foreign_key.referred_schema, foreign_key.referred_table)
                )
                if right_model_id is None:
                    continue
                left_fields = [
                    column_to_field[(table.schema_name, table.name, item)]
                    for item in foreign_key.constrained_columns
                ]
                right_fields = [
                    column_to_field[(foreign_key.referred_schema, foreign_key.referred_table, item)]
                    for item in foreign_key.referred_columns
                ]
                left_is_unique = all(
                    column_index[item].primary_key or column_index[item].unique
                    for item in foreign_key.constrained_columns
                )
                relation_id = _stable_id(
                    "relation", left_model_id, right_model_id, *(foreign_key.constrained_columns)
                )
                suggestions.append(
                    SuggestionPatch(
                        id=_stable_id("suggestion", relation_id),
                        target_kind="relation",
                        target_id=relation_id,
                        changes={
                            "left_model_id": left_model_id,
                            "right_model_id": right_model_id,
                            "join_type": JoinType.LEFT.value,
                            "cardinality": (
                                Cardinality.ONE_TO_ONE.value
                                if left_is_unique
                                else Cardinality.MANY_TO_ONE.value
                            ),
                            "conditions": [
                                {"left_field_id": left, "right_field_id": right}
                                for left, right in zip(left_fields, right_fields, strict=True)
                            ],
                        },
                        source=SuggestionSource.DATABASE_CONSTRAINT,
                        confidence=1.0,
                        reason="由数据库外键与唯一约束推导，仍需人工确认业务 Join 方向和基数",
                        high_impact=True,
                    )
                )

        revision_id = f"rev_{uuid.uuid4().hex}"
        datasets: tuple[DatasetSpec, ...] = ()
        if create_default_dataset:
            # Compatibility for the original V0 all-in-one introspection API.
            # API-first modeling calls this builder with False because a dataset
            # is an explicit query boundary, never a schema-scan side effect.
            datasets = (
                DatasetSpec(
                    id=_stable_id("dataset", project_id, "default"),
                    name="默认数据集",
                    model_ids=tuple(model.id for model in models),
                ),
            )
        draft = SemanticRelease(
            id=revision_id,
            project_id=project_id,
            spec_hash="pending",
            models=tuple(models),
            fields=tuple(fields),
            datasets=datasets,
            revision_id=revision_id,
        )
        draft = draft.model_copy(update={"spec_hash": semantic_release_hash(draft)})
        return RuleModelingResult(semantic_spec=draft, suggestions=tuple(suggestions))

    @staticmethod
    def _field_suggestion(
        model_id: str,
        field_id: str,
        column: SchemaColumnSnapshot,
        *,
        prefill: Prefill,
    ) -> SuggestionPatch:
        constrained = prefill.confidence == 1.0 and prefill.kind is FieldKind.IDENTIFIER
        source = SuggestionSource.DATABASE_CONSTRAINT if constrained else SuggestionSource.RULE
        # 度量和标识符判错的代价最大（静默错数 / Join 扇出），标高影响让人重点看。
        high_impact = prefill.kind in {FieldKind.MEASURE, FieldKind.IDENTIFIER}
        changes: dict[str, object] = {
            "kind": prefill.kind.value,
            "name": column.comment or column.name,
            "description": column.comment,
            "semantic_expr": _quote_identifier(column.name),
        }
        if prefill.identifier_type is not None:
            changes["identifier_type"] = prefill.identifier_type
        if prefill.dimension_type is not None:
            changes["dimension_type"] = prefill.dimension_type
        if prefill.aggregation is not None:
            changes["aggregation"] = prefill.aggregation.value
        confidence = prefill.confidence
        reason = prefill.reason
        return SuggestionPatch(
            id=_stable_id("suggestion", model_id, field_id, "classification"),
            target_kind="field",
            target_id=field_id,
            changes=changes,
            source=source,
            confidence=confidence,
            reason=reason,
            high_impact=high_impact,
        )


def stable_id(prefix: str, *parts: str) -> str:
    return _stable_id(prefix, *parts)


def _stable_id(prefix: str, *parts: str) -> str:
    raw = ":".join((prefix, *parts))
    if len(raw) <= 128:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _quote_identifier(column: str) -> str:
    """Render a physical column name as a parseable SQL identifier.

    ``semantic_expr`` is parsed as SQL downstream, where a digit-leading name
    such as ``500强排名`` reads as a number and resolves to no governed field.
    """

    name = column.strip()
    if name.startswith('"') and name.endswith('"') and len(name) > 1:
        return name
    return '"' + name.replace('"', '""') + '"'
