from __future__ import annotations

from knowflow_analytics.contracts import FieldKind
from knowflow_analytics.modeling.contracts import (
    DimensionDictionaryEligibility,
    DimensionDictionaryEligibilityStatus,
    ModelingRevision,
    SemanticDataProfile,
)


def assess_dimension_dictionary_eligibility(
    *,
    revision: ModelingRevision,
    dimension_ids: tuple[str, ...] | None = None,
    profile: SemanticDataProfile | None = None,
) -> tuple[DimensionDictionaryEligibility, ...]:
    """Classify dictionary targets from governed metadata and observed evidence.

    Dictionary tasks exist for dimension resources, and the same qualification
    runs when a governed dimension is materialized: physical
    identifiers, time dimensions and sensitive resources cannot silently become
    value dictionaries. Dataset exposure is deliberately irrelevant because value
    collection belongs to Dimension creation, before analysis topics are defined.
    No decision depends on a field name.
    """

    dimensions = {item.id: item for item in revision.semantic_spec.dimensions}
    fields = {item.id: item for item in revision.semantic_spec.fields}
    selected_ids = dimension_ids or tuple(item.id for item in revision.semantic_spec.dimensions)
    catalog_dimensions = (
        {item.id: item for item in revision.semantic_catalog.dimensions}
        if revision.semantic_catalog is not None
        else {}
    )
    profile_by_dimension = (
        {item.dimension_id: item for item in profile.dimensions} if profile is not None else {}
    )

    assessments: list[DimensionDictionaryEligibility] = []
    for dimension_id in selected_ids:
        dimension = dimensions.get(dimension_id)
        if dimension is None:
            assessments.append(
                _assessment(
                    dimension_id,
                    DimensionDictionaryEligibilityStatus.INELIGIBLE,
                    "unknown_dimension",
                    "维度不属于当前语义版本，不能建立字典。",
                )
            )
            continue
        field = fields.get(dimension.field_id)
        if field is None or field.model_id != dimension.model_id:
            assessments.append(
                _assessment(
                    dimension_id,
                    DimensionDictionaryEligibilityStatus.INELIGIBLE,
                    "invalid_field_binding",
                    "维度没有绑定当前模型中的物理字段，不能建立字典。",
                )
            )
            continue
        if field.kind is FieldKind.IDENTIFIER or field.dimension_type in {
            "primary_key",
            "foreign_key",
        }:
            assessments.append(
                _assessment(
                    dimension_id,
                    DimensionDictionaryEligibilityStatus.INELIGIBLE,
                    "technical_identifier",
                    "主键或外键继续用于 Join、粒度和明细追溯，但不进入维度值字典。",
                )
            )
            continue
        if dimension.semantic_type == "time" or field.kind is FieldKind.TIME:
            assessments.append(
                _assessment(
                    dimension_id,
                    DimensionDictionaryEligibilityStatus.INELIGIBLE,
                    "time_dimension",
                    "时间值由时间解析器处理，不建立枚举值字典。",
                )
            )
            continue
        if dimension.semantic_type != "categorical" or field.kind is not FieldKind.DIMENSION:
            assessments.append(
                _assessment(
                    dimension_id,
                    DimensionDictionaryEligibilityStatus.INELIGIBLE,
                    "non_categorical_dimension",
                    "只有已确认的分类维度可以建立维度值字典。",
                )
            )
            continue

        catalog_dimension = catalog_dimensions.get(dimension_id)
        if catalog_dimension is not None and catalog_dimension.sensitive_level > 0:
            assessments.append(
                _assessment(
                    dimension_id,
                    DimensionDictionaryEligibilityStatus.REVIEW,
                    "sensitive_dimension",
                    "该维度带有敏感级别，默认不采集，需人工确认后才能读取值。",
                )
            )
            continue

        dimension_profile = profile_by_dimension.get(dimension_id)
        if dimension_profile is not None and dimension_profile.truncated:
            assessments.append(
                _assessment(
                    dimension_id,
                    DimensionDictionaryEligibilityStatus.REVIEW,
                    "high_cardinality",
                    "不同值数量超过当前字典上限，候选不完整，必须人工决定是否采用。",
                    observed_distinct_values=dimension_profile.observed_distinct_values,
                )
            )
            continue
        assessments.append(
            _assessment(
                dimension_id,
                DimensionDictionaryEligibilityStatus.ELIGIBLE,
                "enumerable_business_dimension",
                "普通业务分类维度，可从数据库确定性采集并自动预置完整枚举值。",
                observed_distinct_values=(
                    dimension_profile.observed_distinct_values
                    if dimension_profile is not None
                    else None
                ),
            )
        )
    return tuple(assessments)


def _assessment(
    dimension_id: str,
    status: DimensionDictionaryEligibilityStatus,
    reason_code: str,
    message: str,
    *,
    observed_distinct_values: int | None = None,
) -> DimensionDictionaryEligibility:
    return DimensionDictionaryEligibility(
        dimension_id=dimension_id,
        status=status,
        reason_code=reason_code,
        message=message,
        observed_distinct_values=observed_distinct_values,
    )
