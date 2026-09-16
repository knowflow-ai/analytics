"""发布页就地补全缺失的别名审核。

现场（2026-09-16）：发布门说「queryable resources have no reviewed alias draft: [...]」，
用户不知道该做什么。现在门只指名业务名，发布页一个按钮只为缺失的几项生成别名草稿，
采用后审核记录补齐，不用回建模页整包重跑。
"""

from __future__ import annotations

import pytest
from test_m2_modeling import _application_with_catalog, _catalog

from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.ai_artifacts import reconcile_query_scopes
from knowflow_analytics.modeling.ai_modeller import AliasSuggestionOutput
from knowflow_analytics.modeling.contracts import SemanticAliasReview
from knowflow_analytics.modeling.revision import RevisionConflictError

ORDERS_COUNT = "metric:default_count:model_orders"


class _AliasModeller:
    """记录被问到的资源；只有缺失的那几项才允许进模型。"""

    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []

    def suggest_alias_batch(self, *, resources, **_kwargs):
        self.requested.extend((item["resource_type"], item["resource_id"]) for item in resources)
        return {
            str(item["resource_id"]): AliasSuggestionOutput(
                aliases=("单量",) if item["resource_type"] == "metric" else ("渠道来源",)
            )
            for item in resources
        }


def _queryable_resources(release) -> set[str]:
    dimensions = {item.id: item for item in release.dimensions}
    queryable_dimensions = {
        item_id
        for dataset in release.datasets
        for item_id in dataset.dimension_ids
        if dimensions[item_id].semantic_type != "identifier"
    }
    resources = {f"dimension:{item_id}" for item_id in queryable_dimensions}
    resources |= {
        f"metric:{item_id}" for dataset in release.datasets for item_id in dataset.metric_ids
    }
    resources |= {
        f"dimension_value:{item.id}"
        for item in release.dimension_values
        if item.dimension_id in queryable_dimensions
    }
    return resources


def _site_like_application(modeller: _AliasModeller):
    """AI 补全跑过（有产物哈希），之后订单数量和渠道维度失去了审核记录。"""

    catalog = reconcile_query_scopes(_catalog())
    application = _application_with_catalog(catalog, ai_modeller=modeller)
    revision = application.catalog.get_revision(catalog.revision_id)
    reviewed = _queryable_resources(revision.semantic_spec) - {
        f"metric:{ORDERS_COUNT}",
        "dimension:dimension_channel",
    }
    seeded = revision.model_copy(
        update={
            "ai_modeling_artifact_hash": "sha256:seeded",
            "ai_alias_reviewed_resources": tuple(sorted(reviewed)),
        }
    )
    application.catalog.update_revision(seeded, previous_etag=revision.etag)
    return application, application.catalog.get_revision(catalog.revision_id)


def test_publish_gate_blocks_until_the_two_gaps_are_reviewed() -> None:
    application, revision = _site_like_application(_AliasModeller())

    with pytest.raises(SemanticValidationError) as raised:
        application.validate_revision(revision.id)

    assert raised.value.code == "AI_MODELING_ALIAS_REVIEW_INCOMPLETE"
    assert "订单数量" in str(raised.value)
    assert "渠道" in str(raised.value)


def test_suggesting_completion_only_asks_the_model_about_the_missing_resources() -> None:
    modeller = _AliasModeller()
    application, revision = _site_like_application(modeller)

    completion = application.suggest_alias_completion(
        revision_id=revision.id, expected_etag=revision.etag
    )

    assert completion.revision_etag == revision.etag
    assert {(item.resource_type, item.resource_id) for item in completion.drafts} == {
        ("metric", ORDERS_COUNT),
        ("dimension", "dimension_channel"),
    }
    assert sorted(modeller.requested) == [
        ("dimension", "dimension_channel"),
        ("metric", ORDERS_COUNT),
    ]
    orders = next(item for item in completion.drafts if item.resource_id == ORDERS_COUNT)
    assert orders.resource_name == "订单数量"
    assert "单量" in orders.aliases


def test_applying_the_drafts_records_the_review_and_unblocks_publishing() -> None:
    modeller = _AliasModeller()
    application, revision = _site_like_application(modeller)
    completion = application.suggest_alias_completion(
        revision_id=revision.id, expected_etag=revision.etag
    )

    updated = application.apply_alias_completion(
        revision_id=revision.id,
        expected_etag=revision.etag,
        drafts=tuple(
            SemanticAliasReview(
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                aliases=item.aliases,
                display_name=item.display_name,
            )
            for item in completion.drafts
        ),
    )

    assert updated.etag == revision.etag + 1
    orders = next(item for item in updated.semantic_catalog.metrics if item.id == ORDERS_COUNT)
    assert "单量" in orders.alias.split(",")
    assert f"metric:{ORDERS_COUNT}" in updated.ai_alias_reviewed_resources
    assert "dimension:dimension_channel" in updated.ai_alias_reviewed_resources
    assert set(revision.ai_alias_reviewed_resources) <= set(updated.ai_alias_reviewed_resources)
    validated = application.validate_revision(updated.id)
    assert validated.state.value == "validated"


def test_completion_refuses_a_stale_etag() -> None:
    application, revision = _site_like_application(_AliasModeller())

    with pytest.raises(RevisionConflictError):
        application.suggest_alias_completion(
            revision_id=revision.id, expected_etag=revision.etag + 7
        )
    with pytest.raises(RevisionConflictError):
        application.apply_alias_completion(
            revision_id=revision.id, expected_etag=revision.etag + 7, drafts=()
        )


def test_nothing_missing_means_no_model_call() -> None:
    modeller = _AliasModeller()
    catalog = reconcile_query_scopes(_catalog())
    application = _application_with_catalog(catalog, ai_modeller=modeller)
    revision = application.catalog.get_revision(catalog.revision_id)
    complete = revision.model_copy(
        update={
            "ai_modeling_artifact_hash": "sha256:seeded",
            "ai_alias_reviewed_resources": tuple(
                sorted(_queryable_resources(revision.semantic_spec))
            ),
        }
    )
    application.catalog.update_revision(complete, previous_etag=revision.etag)
    revision = application.catalog.get_revision(catalog.revision_id)

    completion = application.suggest_alias_completion(
        revision_id=revision.id, expected_etag=revision.etag
    )

    assert completion.drafts == ()
    assert modeller.requested == []


def test_the_two_routes_round_trip_through_http() -> None:
    from fastapi.testclient import TestClient

    from knowflow_analytics.api import create_api

    modeller = _AliasModeller()
    application, revision = _site_like_application(modeller)
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": revision.project_id,
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v1",
    }
    base = f"/v1/analytics/projects/{revision.project_id}/revisions/{revision.id}"

    suggested = client.post(
        f"{base}/alias-completion:suggest",
        headers=headers,
        json={"expected_etag": revision.etag},
    )
    assert suggested.status_code == 200, suggested.text
    payload = suggested.json()
    assert payload["revision_etag"] == revision.etag
    assert {item["resource_id"] for item in payload["drafts"]} == {
        ORDERS_COUNT,
        "dimension_channel",
    }

    applied = client.post(
        f"{base}/alias-completion:apply",
        headers=headers,
        json={
            "expected_etag": revision.etag,
            "drafts": [
                {
                    "resource_type": item["resource_type"],
                    "resource_id": item["resource_id"],
                    "aliases": item["aliases"],
                    "display_name": item["display_name"],
                }
                for item in payload["drafts"]
            ],
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["etag"] == revision.etag + 1
    assert f"metric:{ORDERS_COUNT}" in applied.json()["ai_alias_reviewed_resources"]

    stale = client.post(
        f"{base}/alias-completion:suggest",
        headers=headers,
        json={"expected_etag": revision.etag},
    )
    assert stale.status_code == 409, stale.text
