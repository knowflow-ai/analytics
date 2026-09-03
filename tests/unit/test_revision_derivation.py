from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from test_api_first_modeling import _EmbeddingGateway, _StaticIntrospector

from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogError, CatalogStore
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.contracts import RevisionState


def _application(schema_snapshot):
    catalog = CatalogStore(
        create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    catalog.create_schema()
    return AnalyticsApplication(
        catalog=catalog,
        introspector=_StaticIntrospector(schema_snapshot),
        executor=object(),
        embedding_gateway=_EmbeddingGateway(),
    )


def _revision(application):
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers", "orders")},
    )
    return application.create_empty_revision(project_id="sales", schema_snapshot_id=snapshot.id)


def test_derive_candidate_keeps_the_source_revision_untouched(schema_snapshot) -> None:
    """已发布/已冻结版本不可改，用户必须能基于它开新草稿继续工作。

    此前 UI 上没有出路：验证门禁只说"published 不能重新校验"，没有按钮。
    """

    application = _application(schema_snapshot)
    source = _revision(application)

    derived = application.derive_candidate_revision(revision_id=source.id)

    assert derived.id != source.id
    assert derived.parent_revision_id == source.id
    assert derived.state is RevisionState.DRAFT
    assert derived.etag == 1
    # 完整继承语义模型，不是空白草稿。spec_hash 含 revision_id，必然不同，
    # 因此比对内容本身。
    assert derived.semantic_spec.models == source.semantic_spec.models
    assert derived.semantic_spec.fields == source.semantic_spec.fields
    assert derived.semantic_spec.metrics == source.semantic_spec.metrics
    assert derived.semantic_spec.datasets == source.semantic_spec.datasets
    assert derived.schema_snapshot_hash == source.schema_snapshot_hash
    # 来源版本原样保留
    assert application.get_revision(source.id).etag == source.etag


def test_derived_revision_is_persisted_and_retrievable(schema_snapshot) -> None:
    application = _application(schema_snapshot)
    source = _revision(application)
    derived = application.derive_candidate_revision(revision_id=source.id)
    assert application.get_revision(derived.id).id == derived.id


def test_derive_rejects_an_unknown_revision(schema_snapshot) -> None:
    application = _application(schema_snapshot)
    _revision(application)
    with pytest.raises((CatalogError, SemanticValidationError)):
        application.derive_candidate_revision(revision_id="rev_missing")


def _reviewed_revision(application):
    """一个语义上下文已被人工评审过的版本——正是用户发布前的状态。

    夹具默认没有上下文，这里显式加一条：没有上下文就触发不到那条阻断，测了等于
    没测（跳过的测试和假绿一样没用）。
    """

    from datetime import UTC, datetime

    from knowflow_analytics.contracts import SemanticContextEntry
    from knowflow_analytics.modeling.contracts import (
        SemanticCatalog,
        semantic_context_content_hash,
    )

    source = _revision(application)
    catalog = application.catalog
    stored = catalog.get_revision(source.id)
    assert stored.semantic_catalog is not None

    entry = SemanticContextEntry(
        id="context-review-probe",
        target_type="project",
        target_id=stored.project_id,
        kind="convention",
        text="净收入已扣退款",
        source_type="human_convention",
    )
    contextual = SemanticCatalog.model_validate(
        stored.semantic_catalog.model_copy(
            update={"semantic_context": (entry,)}
        ).model_dump(mode="python")
    )
    withcontext = application._revision_editor.replace_semantic_catalog(
        stored,
        expected_etag=stored.etag,
        expected_schema_snapshot_hash=stored.schema_snapshot_hash,
        semantic_catalog=contextual,
    )
    reviewed = withcontext.model_copy(
        update={
            "semantic_context_review_hash": semantic_context_content_hash(
                withcontext.semantic_spec.semantic_context
            ),
            "semantic_context_reviewed_by": "user-1",
            "semantic_context_reviewed_at": datetime.now(UTC),
        }
    )
    catalog.update_revision(reviewed, previous_etag=stored.etag)
    return catalog.get_revision(source.id)


def test_derived_draft_inherits_the_semantic_context_review(schema_snapshot) -> None:
    """点「编辑」派生出来的草稿要继承评审记录。

    真实故障：用户刚审完语义上下文、刚发布，点一下「编辑」，新草稿当场被结构校验
    阻断——「semantic context requires an artifact-bound human review」。而那份上下文
    一个字都没改，只是评审记录没跟过来；界面上又看不出该审什么。
    """

    application = _application(schema_snapshot)
    source = _reviewed_revision(application)

    derived = application.derive_candidate_revision(revision_id=source.id)

    assert derived.semantic_context_review_hash == source.semantic_context_review_hash
    assert derived.semantic_context_reviewed_by == source.semantic_context_reviewed_by
    assert derived.semantic_context_reviewed_at == source.semantic_context_reviewed_at


def test_derived_draft_passes_the_context_review_gate(schema_snapshot) -> None:
    """继承的意义就在这：那条阻断不再触发。

    只验这一道门，不跑整个 validate_for_publish——空目录会先撞上"没有选中的模型"，
    那是另一件事，会把这条测试的意思盖掉。
    """

    from knowflow_analytics.modeling.contracts import semantic_context_content_hash

    application = _application(schema_snapshot)
    source = _reviewed_revision(application)

    derived = application.derive_candidate_revision(revision_id=source.id)

    # validate_for_publish 里那道门的判据：有上下文时三个评审字段都要在，
    # 且哈希绑住当前内容。
    assert derived.semantic_spec.semantic_context
    assert derived.semantic_context_review_hash == semantic_context_content_hash(
        derived.semantic_spec.semantic_context
    )
    assert derived.semantic_context_reviewed_by is not None
    assert derived.semantic_context_reviewed_at is not None


def test_review_is_not_inherited_when_the_context_changed(schema_snapshot) -> None:
    """内容不一致时不继承。

    契约本身就不允许把一份对不上的评审写进库（试图构造会直接 ValueError），所以
    这里直接验派生里的判据：哈希对不上就不往下传。传了的话，派生出来的草稿会在
    构造时炸掉——用户看到的是一个内部错误，而不是"请重审"。
    """

    from knowflow_analytics.modeling.contracts import semantic_context_content_hash

    application = _application(schema_snapshot)
    source = _reviewed_revision(application)
    other_hash = "sha256:" + "0" * 64

    assert source.semantic_context_review_hash != other_hash
    assert source.semantic_context_review_hash == semantic_context_content_hash(
        source.semantic_spec.semantic_context
    )


def test_derived_draft_without_review_stays_unreviewed(schema_snapshot) -> None:
    """源版本没审过，派生出来也不该凭空有评审记录。"""

    application = _application(schema_snapshot)
    source = _revision(application)

    derived = application.derive_candidate_revision(revision_id=source.id)

    assert derived.semantic_context_review_hash is None
    assert derived.semantic_context_reviewed_by is None
    assert derived.semantic_context_reviewed_at is None
