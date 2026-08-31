from __future__ import annotations

from knowflow_analytics.contracts import DimensionSpec
from knowflow_analytics.modeling.contracts import (
    DimensionDictionaryEligibilityStatus,
    ModelingRevision,
)
from knowflow_analytics.modeling.dimension_dictionary_eligibility import (
    assess_dimension_dictionary_eligibility,
)


def test_identifier_and_time_dimensions_are_not_dictionary_targets(sales_release):
    identifier_dimension = DimensionSpec(
        id="customer_key",
        name="客户标识",
        model_id="orders",
        field_id="orders.customer_id",
    )
    release = sales_release.model_copy(
        update={"dimensions": sales_release.dimensions + (identifier_dimension,)}
    )
    revision = ModelingRevision(
        id="revision-eligibility",
        project_id=release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=1,
        semantic_spec=release,
    )

    assessments = {
        item.dimension_id: item
        for item in assess_dimension_dictionary_eligibility(revision=revision)
    }

    assert assessments["region"].status is DimensionDictionaryEligibilityStatus.ELIGIBLE
    assert assessments["customer_key"].status is DimensionDictionaryEligibilityStatus.INELIGIBLE
    assert assessments["customer_key"].reason_code == "technical_identifier"
    assert assessments["order_date"].status is DimensionDictionaryEligibilityStatus.INELIGIBLE
    assert assessments["order_date"].reason_code == "time_dimension"


def test_unexposed_business_dimension_is_eligible_before_dataset_creation(sales_release):
    dataset = sales_release.datasets[0].model_copy(
        update={
            "dimension_ids": tuple(
                item for item in sales_release.datasets[0].dimension_ids if item != "product"
            )
        }
    )
    release = sales_release.model_copy(update={"datasets": (dataset,)})
    revision = ModelingRevision(
        id="revision-unexposed",
        project_id=release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=1,
        semantic_spec=release,
    )

    assessments = {
        item.dimension_id: item
        for item in assess_dimension_dictionary_eligibility(revision=revision)
    }

    assert assessments["product"].status is DimensionDictionaryEligibilityStatus.ELIGIBLE
    assert assessments["product"].reason_code == "enumerable_business_dimension"
