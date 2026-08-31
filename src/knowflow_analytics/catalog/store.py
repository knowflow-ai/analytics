from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import Field
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from knowflow_analytics.contracts import FrozenModel, SemanticRelease
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.modeling.contracts import (
    DimensionDictionaryPreview,
    DimensionDictionaryStatus,
    ModelingProposal,
    ModelingProposalStatus,
    ModelingRevision,
    ModelingRunStatus,
    ModelingSuggestionRun,
    RevisionState,
    SchemaSnapshot,
)
from knowflow_analytics.modeling.domain import DomainGovernance, DomainLifecycle
from knowflow_analytics.modeling.drift import SchemaDriftReport
from knowflow_analytics.modeling.layout import ModelGraphLayout
from knowflow_analytics.modeling.product import ModelingPlan, ModelingPlanStatus
from knowflow_analytics.modeling.quality import (
    ModelingQualityReport,
    ModelingQualityReportStatus,
)
from knowflow_analytics.semantic.index import IndexState, SemanticIndexSnapshot

if TYPE_CHECKING:
    from knowflow_analytics.evaluation.contracts import EvaluationReport, GoldenSuiteRecord

metadata = MetaData()

projects = Table(
    "analytics_project",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("name", String(256), nullable=False),
    Column("active_release_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

domain_governance = Table(
    "analytics_domain_governance",
    metadata,
    Column("project_id", String(128), primary_key=True),
    Column("etag", Integer, nullable=False),
    Column("lifecycle", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

model_graph_layouts = Table(
    "analytics_model_graph_layout",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False, index=True),
    Column("etag", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

schema_snapshots = Table(
    "analytics_schema_snapshot",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("content_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

revisions = Table(
    "analytics_revision",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("parent_revision_id", String(128), nullable=True),
    Column("schema_snapshot_hash", String(128), nullable=False),
    Column("etag", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("spec_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

modeling_runs = Table(
    "analytics_modeling_run",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False, index=True),
    Column("revision_etag", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("input_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

modeling_jobs = Table(
    "analytics_modeling_job",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

modeling_plans = Table(
    "analytics_modeling_plan",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False, index=True),
    Column("revision_etag", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("input_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

modeling_proposals = Table(
    "analytics_modeling_proposal",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False, index=True),
    Column("suggestion_run_id", String(128), nullable=False, unique=True),
    Column("revision_etag", Integer, nullable=False),
    Column("etag", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("proposal_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

dimension_dictionary_previews = Table(
    "analytics_dimension_dictionary_preview",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False, index=True),
    Column("revision_etag", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("semantic_spec_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

modeling_quality_reports = Table(
    "analytics_modeling_quality_report",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False, index=True),
    Column("revision_etag", Integer, nullable=False),
    Column("semantic_spec_hash", String(128), nullable=False, index=True),
    Column("etag", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("content_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

schema_drift_reports = Table(
    "analytics_schema_drift_report",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False, index=True),
    Column("revision_etag", Integer, nullable=False),
    Column("content_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

index_snapshots = Table(
    "analytics_index_snapshot",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("release_spec_hash", String(128), nullable=False),
    Column("content_hash", String(128), nullable=False),
    Column("state", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

releases = Table(
    "analytics_release",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False),
    Column("spec_hash", String(128), nullable=False),
    Column("index_snapshot_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

evaluation_reports = Table(
    "analytics_evaluation_report",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("spec_hash", String(128), nullable=False, index=True),
    Column("suite_id", String(128), nullable=False),
    Column("gate_passed", String(8), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

golden_suites = Table(
    "analytics_golden_suite",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("revision_id", String(128), nullable=False, index=True),
    Column("revision_etag", Integer, nullable=False),
    Column("semantic_spec_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

query_failures = Table(
    "analytics_query_failures",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor_id", String(128), nullable=False, index=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("release_id", String(128), nullable=False),
    Column("spec_hash", String(128), nullable=False),
    Column("index_snapshot_id", String(128), nullable=False),
    Column("stage", String(64), nullable=False, index=True),
    Column("code", String(128), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

confirmation_memories = Table(
    "analytics_confirmation_memory",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("actor_id", String(128), nullable=False, index=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("release_id", String(128), nullable=False, index=True),
    Column("spec_hash", String(128), nullable=False),
    Column("index_snapshot_id", String(128), nullable=False),
    Column("normalized_phrase", String(4000), nullable=False),
    Column("candidate_set_hash", String(128), nullable=False),
    Column("exact_context_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)

query_history = Table(
    "analytics_query_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor_id", String(128), nullable=False, index=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("conversation_id", String(128), nullable=False, index=True),
    Column("release_id", String(128), nullable=False),
    Column("spec_hash", String(128), nullable=False),
    Column("index_snapshot_id", String(128), nullable=False),
    Column("dataset_id", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

query_diagnostics = Table(
    "analytics_query_diagnostic",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("query_id", String(128), nullable=False, index=True),
    Column("actor_id", String(128), nullable=False, index=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("permission_scope_hash", String(128), nullable=False, index=True),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class ProjectRecord(FrozenModel):
    id: str
    name: str
    active_release_id: str | None = None
    # 建库起就落在 analytics_project.created_at，此前只用于排序、从未读出。
    # 必填而非可选：漏传会在构造时失败，而不是让列表静默少一列。
    created_at: datetime
    # 仅列表填充：最近更新的 revision，让前端一次调用就能打开项目。
    latest_revision_id: str | None = None


class ReleaseSummary(FrozenModel):
    """发布历史里的一行。不带 SemanticRelease 正文 —— 列表只需要知道有哪些版本。"""

    id: str
    revision_id: str
    spec_hash: str
    status: str = Field(pattern="^(active|retired)$")
    created_at: datetime


class PublishedRelease(FrozenModel):
    release: SemanticRelease
    index_snapshot: SemanticIndexSnapshot
    status: str = Field(pattern="^(active|retired)$")


class CatalogError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "CATALOG_ERROR") -> None:
        super().__init__(message, code=code, stage="CATALOG")


def _schema_snapshot_storage_id(project_id: str, snapshot_id: str) -> str:
    """Scope a content-addressed snapshot to its owning analytics project."""

    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"knowflow-analytics:{project_id}:{snapshot_id}").hex
    return f"schema_scope_{digest}"


def _model_graph_layout_id(project_id: str, revision_id: str) -> str:
    digest = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"knowflow-analytics:model-graph:{project_id}:{revision_id}",
    ).hex
    return f"graph_{digest}"


def _query_diagnostic_advisory_lock_id(actor_id: str, project_id: str) -> int:
    digest = hashlib.blake2b(
        f"{actor_id}\0{project_id}".encode(),
        digest_size=8,
        person=b"kf-qdiag-lock",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


class CatalogStore:
    """Small V0 catalog with optimistic revisions and atomic release activation."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._query_diagnostic_purge_lock = threading.Lock()
        self._query_diagnostic_last_purge = 0.0

    def create_schema(self) -> None:
        metadata.create_all(self._engine)

    def create_project(self, *, name: str, project_id: str | None = None) -> ProjectRecord:
        now = datetime.now(UTC)
        record = ProjectRecord(
            id=project_id or f"prj_{uuid.uuid4().hex}",
            name=name,
            created_at=now,
        )
        governance = DomainGovernance(
            project_id=record.id,
            etag=1,
            updated_by="system",
            updated_at=now,
        )
        with self._engine.begin() as connection:
            connection.execute(
                insert(projects).values(
                    id=record.id,
                    name=record.name,
                    active_release_id=None,
                    created_at=now,
                )
            )
            connection.execute(
                insert(domain_governance).values(
                    project_id=record.id,
                    etag=governance.etag,
                    lifecycle=governance.lifecycle.value,
                    payload=governance.model_dump(mode="json"),
                    updated_at=now,
                )
            )
        return record

    def list_projects(self, *, id_prefix: str, limit: int = 200) -> tuple[ProjectRecord, ...]:
        """按 id 前缀列项目，最新在前。

        项目归属不在本服务：BFF 把 actor 的 HMAC 标签编进 id 前缀
        （prj_u{owner}_{nonce}），这里只按前缀过滤，不认识 actor。
        """

        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        projects.c.id,
                        projects.c.name,
                        projects.c.active_release_id,
                        projects.c.created_at,
                    )
                    .where(projects.c.id.like(f"{id_prefix}%"))
                    .order_by(projects.c.created_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            project_ids = [row["id"] for row in rows]
            latest: dict[str, str] = {}
            if project_ids:
                # 每个项目最近更新的 revision；打开项目要从它进入。
                for rev in (
                    connection.execute(
                        select(revisions.c.project_id, revisions.c.id)
                        .where(revisions.c.project_id.in_(project_ids))
                        .order_by(revisions.c.updated_at.desc())
                    )
                    .mappings()
                    .all()
                ):
                    latest.setdefault(rev["project_id"], rev["id"])
        return tuple(ProjectRecord(**row, latest_revision_id=latest.get(row["id"])) for row in rows)

    def get_project(self, project_id: str) -> ProjectRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        projects.c.id,
                        projects.c.name,
                        projects.c.active_release_id,
                        projects.c.created_at,
                    ).where(projects.c.id == project_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise CatalogError("analytics project was not found", code="PROJECT_NOT_FOUND")
        return ProjectRecord.model_validate(dict(row))

    def get_domain_governance(self, project_id: str) -> DomainGovernance:
        project = self.get_project(project_id)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(domain_governance.c.payload).where(
                    domain_governance.c.project_id == project_id
                )
            ).scalar_one_or_none()
        if payload is not None:
            return DomainGovernance.model_validate(payload)
        # Existing deployments may predate this M3 table. Preserve their online
        # behavior while exposing an optimistic baseline for the first update.
        return DomainGovernance(
            project_id=project_id,
            lifecycle=(
                DomainLifecycle.ONLINE
                if project.active_release_id is not None
                else DomainLifecycle.INITIALIZED
            ),
            etag=1,
            updated_by="system-migration",
            updated_at=datetime.now(UTC),
        )

    def update_domain_governance(
        self,
        *,
        project_id: str,
        expected_etag: int,
        classifications: tuple[str, ...],
        lifecycle: DomainLifecycle,
        updated_by: str,
        parent_project_id: str | None = None,
    ) -> DomainGovernance:
        project = self.get_project(project_id)
        if parent_project_id == project_id:
            raise CatalogError(
                "analytics domain cannot be its own parent",
                code="DOMAIN_PARENT_INVALID",
            )
        if parent_project_id is not None:
            self.get_project(parent_project_id)
        if lifecycle is DomainLifecycle.ONLINE and project.active_release_id is None:
            raise CatalogError(
                "analytics domain requires an active release before going online",
                code="DOMAIN_RELEASE_REQUIRED",
            )
        if lifecycle is DomainLifecycle.INITIALIZED and project.active_release_id is not None:
            raise CatalogError(
                "a published analytics domain cannot return to initialized",
                code="DOMAIN_LIFECYCLE_INVALID",
            )
        current = self.get_domain_governance(project_id)
        if current.etag != expected_etag:
            raise CatalogError(
                "analytics domain governance changed before update",
                code="DOMAIN_ETAG_CONFLICT",
            )
        now = datetime.now(UTC)
        updated = DomainGovernance(
            project_id=project_id,
            parent_project_id=parent_project_id,
            classifications=classifications,
            lifecycle=lifecycle,
            etag=current.etag + 1,
            updated_by=updated_by,
            updated_at=now,
        )
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(domain_governance.c.etag)
                .where(domain_governance.c.project_id == project_id)
                .with_for_update()
            ).scalar_one_or_none()
            if existing is None:
                if expected_etag != 1:
                    raise CatalogError(
                        "analytics domain governance changed before update",
                        code="DOMAIN_ETAG_CONFLICT",
                    )
                connection.execute(
                    insert(domain_governance).values(
                        project_id=project_id,
                        etag=updated.etag,
                        lifecycle=updated.lifecycle.value,
                        payload=updated.model_dump(mode="json"),
                        updated_at=now,
                    )
                )
            else:
                result = connection.execute(
                    update(domain_governance)
                    .where(
                        domain_governance.c.project_id == project_id,
                        domain_governance.c.etag == expected_etag,
                    )
                    .values(
                        etag=updated.etag,
                        lifecycle=updated.lifecycle.value,
                        payload=updated.model_dump(mode="json"),
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    raise CatalogError(
                        "analytics domain governance changed before update",
                        code="DOMAIN_ETAG_CONFLICT",
                    )
        return updated

    def get_model_graph_layout(
        self,
        *,
        project_id: str,
        revision_id: str,
    ) -> ModelGraphLayout | None:
        layout_id = _model_graph_layout_id(project_id, revision_id)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(model_graph_layouts.c.payload).where(
                    model_graph_layouts.c.id == layout_id,
                    model_graph_layouts.c.project_id == project_id,
                    model_graph_layouts.c.revision_id == revision_id,
                )
            ).scalar_one_or_none()
        return None if payload is None else ModelGraphLayout.model_validate(payload)

    def save_model_graph_layout(
        self,
        layout: ModelGraphLayout,
        *,
        expected_etag: int,
    ) -> ModelGraphLayout:
        self.get_project(layout.project_id)
        layout_id = _model_graph_layout_id(layout.project_id, layout.revision_id)
        saved = layout.model_copy(update={"etag": expected_etag + 1})
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(model_graph_layouts.c.etag)
                .where(model_graph_layouts.c.id == layout_id)
                .with_for_update()
            ).scalar_one_or_none()
            if existing is None:
                if expected_etag != 0:
                    raise CatalogError(
                        "model graph layout changed before update",
                        code="MODEL_GRAPH_LAYOUT_ETAG_CONFLICT",
                    )
                connection.execute(
                    insert(model_graph_layouts).values(
                        id=layout_id,
                        project_id=layout.project_id,
                        revision_id=layout.revision_id,
                        etag=saved.etag,
                        payload=saved.model_dump(mode="json"),
                        updated_at=saved.updated_at,
                    )
                )
            else:
                if existing != expected_etag:
                    raise CatalogError(
                        "model graph layout changed before update",
                        code="MODEL_GRAPH_LAYOUT_ETAG_CONFLICT",
                    )
                result = connection.execute(
                    update(model_graph_layouts)
                    .where(
                        model_graph_layouts.c.id == layout_id,
                        model_graph_layouts.c.etag == expected_etag,
                    )
                    .values(
                        etag=saved.etag,
                        payload=saved.model_dump(mode="json"),
                        updated_at=saved.updated_at,
                    )
                )
                if result.rowcount != 1:
                    raise CatalogError(
                        "model graph layout changed before update",
                        code="MODEL_GRAPH_LAYOUT_ETAG_CONFLICT",
                    )
        return saved

    def save_schema_snapshot(self, *, project_id: str, snapshot: SchemaSnapshot) -> None:
        self.get_project(project_id)
        storage_id = _schema_snapshot_storage_id(project_id, snapshot.id)
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(schema_snapshots).values(
                        id=storage_id,
                        project_id=project_id,
                        content_hash=snapshot.content_hash,
                        payload=snapshot.model_dump(mode="json"),
                        created_at=datetime.now(UTC),
                    )
                )
            return
        except IntegrityError:
            # A content-addressed snapshot may be inserted concurrently. Verify the
            # winner instead of treating an unrelated identifier collision as success.
            pass
        with self._engine.connect() as connection:
            existing = (
                connection.execute(
                    select(
                        schema_snapshots.c.project_id,
                        schema_snapshots.c.payload,
                    ).where(schema_snapshots.c.id == storage_id)
                )
                .mappings()
                .one_or_none()
            )
        if existing is None:
            raise CatalogError(
                "schema snapshot could not be persisted",
                code="SNAPSHOT_WRITE_FAILED",
            )
        persisted = SchemaSnapshot.model_validate(existing["payload"])
        if (
            existing["project_id"] != project_id
            or persisted.model_copy(update={"captured_at": snapshot.captured_at}) != snapshot
        ):
            raise CatalogError(
                "schema snapshot identifier collision",
                code="SNAPSHOT_CONFLICT",
            )

    def get_schema_snapshot(self, snapshot_id: str, *, project_id: str) -> SchemaSnapshot:
        storage_id = _schema_snapshot_storage_id(project_id, snapshot_id)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(schema_snapshots.c.payload).where(
                    schema_snapshots.c.id == storage_id,
                    schema_snapshots.c.project_id == project_id,
                )
            ).scalar_one_or_none()
        if payload is None:
            raise CatalogError("schema snapshot was not found", code="SNAPSHOT_NOT_FOUND")
        return SchemaSnapshot.model_validate(payload)

    def save_revision(self, revision: ModelingRevision) -> None:
        self._require_catalog_revision(revision)
        now = datetime.now(UTC)
        with self._engine.begin() as connection:
            connection.execute(
                insert(revisions).values(
                    id=revision.id,
                    project_id=revision.project_id,
                    parent_revision_id=revision.parent_revision_id,
                    schema_snapshot_hash=revision.schema_snapshot_hash,
                    etag=revision.etag,
                    state=revision.state.value,
                    spec_hash=revision.semantic_spec.spec_hash,
                    payload=revision.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )

    def update_revision(self, revision: ModelingRevision, *, previous_etag: int) -> None:
        self._require_catalog_revision(revision)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(revisions)
                .where(revisions.c.id == revision.id, revisions.c.etag == previous_etag)
                .values(
                    etag=revision.etag,
                    state=revision.state.value,
                    spec_hash=revision.semantic_spec.spec_hash,
                    payload=revision.model_dump(mode="json"),
                    updated_at=datetime.now(UTC),
                )
            )
        if result.rowcount != 1:
            raise CatalogError("revision was concurrently modified", code="REVISION_ETAG_CONFLICT")

    @staticmethod
    def _require_catalog_revision(revision: ModelingRevision) -> None:
        if revision.semantic_catalog is None:
            raise CatalogError(
                "persisted modeling revisions require an authoritative semantic catalog",
                code="MODELING_CATALOG_REQUIRED",
            )

    def get_revision(self, revision_id: str) -> ModelingRevision:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(revisions.c.payload).where(revisions.c.id == revision_id)
            ).scalar_one_or_none()
        if payload is None:
            raise CatalogError("modeling revision was not found", code="REVISION_NOT_FOUND")
        revision = ModelingRevision.model_validate(payload)
        self._require_catalog_revision(revision)
        return revision

    def get_latest_revision(self, *, project_id: str) -> ModelingRevision | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(revisions.c.payload)
                .where(revisions.c.project_id == project_id)
                .order_by(revisions.c.updated_at.desc(), revisions.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if payload is None:
            return None
        revision = ModelingRevision.model_validate(payload)
        self._require_catalog_revision(revision)
        return revision

    def save_modeling_job(self, job) -> None:
        from knowflow_analytics.modeling.jobs import ModelingJob

        validated = ModelingJob.model_validate(job)
        with self._engine.begin() as connection:
            connection.execute(
                insert(modeling_jobs).values(
                    id=validated.id,
                    project_id=validated.project_id,
                    revision_id=validated.revision_id,
                    status=validated.status.value,
                    payload=validated.model_dump(mode="json"),
                    created_at=validated.created_at,
                    updated_at=validated.updated_at,
                )
            )

    def update_modeling_job(self, job) -> None:
        from knowflow_analytics.modeling.jobs import ModelingJob

        validated = ModelingJob.model_validate(job)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(modeling_jobs)
                .where(modeling_jobs.c.id == validated.id)
                .values(
                    status=validated.status.value,
                    payload=validated.model_dump(mode="json"),
                    updated_at=validated.updated_at,
                )
            )
            if result.rowcount != 1:
                raise CatalogError("modeling job was not found")

    def get_modeling_job(self, job_id: str):
        from knowflow_analytics.modeling.jobs import ModelingJob

        with self._engine.connect() as connection:
            row = connection.execute(
                select(modeling_jobs.c.payload).where(modeling_jobs.c.id == job_id)
            ).scalar_one_or_none()
        if row is None:
            raise CatalogError("modeling job was not found")
        return ModelingJob.model_validate(row)

    def save_modeling_run(self, run: ModelingSuggestionRun) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(modeling_runs).values(
                    id=run.id,
                    project_id=run.project_id,
                    revision_id=run.revision_id,
                    revision_etag=run.revision_etag,
                    status=run.status.value,
                    input_hash=run.input_hash,
                    payload=run.model_dump(mode="json"),
                    created_at=run.created_at,
                )
            )

    def get_modeling_run(self, run_id: str) -> ModelingSuggestionRun:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(modeling_runs.c.payload).where(modeling_runs.c.id == run_id)
            ).scalar_one_or_none()
        if payload is None:
            raise CatalogError("modeling run was not found", code="MODELING_RUN_NOT_FOUND")
        return ModelingSuggestionRun.model_validate(payload)

    def save_modeling_run_and_proposal(
        self,
        *,
        run: ModelingSuggestionRun,
        proposal: ModelingProposal,
    ) -> None:
        """Persist the one-click AI run and its complete Proposal atomically."""

        if (
            proposal.suggestion_run_id != run.id
            or proposal.project_id != run.project_id
            or proposal.revision_id != run.revision_id
            or proposal.revision_etag != run.revision_etag
        ):
            raise CatalogError(
                "modeling proposal belongs to another suggestion run",
                code="MODELING_PROPOSAL_RUN_MISMATCH",
            )
        with self._engine.begin() as connection:
            connection.execute(
                insert(modeling_runs).values(
                    id=run.id,
                    project_id=run.project_id,
                    revision_id=run.revision_id,
                    revision_etag=run.revision_etag,
                    status=run.status.value,
                    input_hash=run.input_hash,
                    payload=run.model_dump(mode="json"),
                    created_at=run.created_at,
                )
            )
            connection.execute(
                insert(modeling_proposals).values(
                    id=proposal.id,
                    project_id=proposal.project_id,
                    revision_id=proposal.revision_id,
                    suggestion_run_id=proposal.suggestion_run_id,
                    revision_etag=proposal.revision_etag,
                    etag=proposal.etag,
                    status=proposal.status.value,
                    proposal_hash=proposal.proposal_hash,
                    payload=proposal.model_dump(mode="json"),
                    created_at=proposal.created_at,
                    updated_at=proposal.updated_at,
                )
            )

    def get_modeling_proposal(self, proposal_id: str) -> ModelingProposal:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(modeling_proposals.c.payload).where(modeling_proposals.c.id == proposal_id)
            ).scalar_one_or_none()
        if payload is None:
            raise CatalogError(
                "modeling proposal was not found",
                code="MODELING_PROPOSAL_NOT_FOUND",
            )
        return ModelingProposal.model_validate(payload)

    def save_schema_drift_report(self, report: SchemaDriftReport) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(schema_drift_reports).values(
                    id=report.id,
                    project_id=report.project_id,
                    revision_id=report.revision_id,
                    revision_etag=report.revision_etag,
                    content_hash=report.content_hash,
                    payload=report.model_dump(mode="json"),
                    created_at=report.created_at,
                )
            )

    def get_schema_drift_report(self, report_id: str) -> SchemaDriftReport:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(schema_drift_reports.c.payload).where(schema_drift_reports.c.id == report_id)
            ).scalar_one_or_none()
        if payload is None:
            raise CatalogError(
                "schema drift report was not found",
                code="SCHEMA_DRIFT_REPORT_NOT_FOUND",
            )
        return SchemaDriftReport.model_validate(payload)

    def save_modeling_quality_report(self, report: ModelingQualityReport) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(modeling_quality_reports).values(
                    id=report.id,
                    project_id=report.project_id,
                    revision_id=report.revision_id,
                    revision_etag=report.revision_etag,
                    semantic_spec_hash=report.semantic_spec_hash,
                    etag=report.etag,
                    status=report.status.value,
                    content_hash=report.content_hash,
                    payload=report.model_dump(mode="json"),
                    created_at=report.created_at,
                )
            )

    def get_modeling_quality_report(self, report_id: str) -> ModelingQualityReport:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(modeling_quality_reports.c.payload).where(
                    modeling_quality_reports.c.id == report_id
                )
            ).scalar_one_or_none()
        if payload is None:
            raise CatalogError(
                "modeling quality report was not found",
                code="MODELING_QUALITY_REPORT_NOT_FOUND",
            )
        return ModelingQualityReport.model_validate(payload)

    def get_latest_modeling_quality_report(
        self, semantic_spec_hash: str
    ) -> ModelingQualityReport | None:
        """Return the newest quality report produced for one semantic spec hash.

        Publication is gated on the spec hash rather than the revision id so that
        editing a revision after review invalidates the evidence instead of
        silently carrying an obsolete verdict forward.
        """

        with self._engine.connect() as connection:
            payload = connection.execute(
                select(modeling_quality_reports.c.payload)
                .where(modeling_quality_reports.c.semantic_spec_hash == semantic_spec_hash)
                .order_by(modeling_quality_reports.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        return ModelingQualityReport.model_validate(payload) if payload is not None else None

    def review_modeling_quality_report(
        self,
        report: ModelingQualityReport,
        *,
        previous_etag: int,
        previous_content_hash: str,
    ) -> None:
        if report.status is not ModelingQualityReportStatus.REVIEWED:
            raise CatalogError(
                "quality report review status is invalid",
                code="MODELING_QUALITY_REVIEW_INVALID",
            )
        with self._engine.begin() as connection:
            result = connection.execute(
                update(modeling_quality_reports)
                .where(
                    modeling_quality_reports.c.id == report.id,
                    modeling_quality_reports.c.etag == previous_etag,
                    modeling_quality_reports.c.content_hash == previous_content_hash,
                    modeling_quality_reports.c.status
                    == ModelingQualityReportStatus.COMPLETED.value,
                )
                .values(
                    etag=report.etag,
                    status=report.status.value,
                    content_hash=report.content_hash,
                    payload=report.model_dump(mode="json"),
                )
            )
        if result.rowcount != 1:
            raise CatalogError(
                "modeling quality report was concurrently reviewed",
                code="MODELING_QUALITY_REPORT_CONFLICT",
            )

    def update_modeling_proposal(
        self,
        proposal: ModelingProposal,
        *,
        previous_etag: int,
        previous_hash: str,
    ) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(modeling_proposals)
                .where(
                    modeling_proposals.c.id == proposal.id,
                    modeling_proposals.c.etag == previous_etag,
                    modeling_proposals.c.proposal_hash == previous_hash,
                    modeling_proposals.c.status == ModelingProposalStatus.DRAFT.value,
                )
                .values(
                    etag=proposal.etag,
                    proposal_hash=proposal.proposal_hash,
                    payload=proposal.model_dump(mode="json"),
                    updated_at=proposal.updated_at,
                )
            )
        if result.rowcount != 1:
            raise CatalogError(
                "modeling proposal was concurrently modified",
                code="MODELING_PROPOSAL_CONFLICT",
            )

    def save_modeling_plan(self, plan: ModelingPlan) -> ModelingPlan:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(modeling_plans).values(
                        id=plan.id,
                        project_id=plan.project_id,
                        revision_id=plan.revision_id,
                        revision_etag=plan.revision_etag,
                        status=plan.status.value,
                        input_hash=plan.input_hash,
                        payload=plan.model_dump(mode="json"),
                        created_at=plan.created_at,
                    )
                )
            return plan
        except IntegrityError as exc:
            persisted = self.get_modeling_plan(plan.id)
            # The identifier is content-addressed. A repeated POST after a
            # non-mutating review must return the persisted review audit.
            normalized = persisted.model_copy(
                update={
                    "status": plan.status,
                    "created_at": plan.created_at,
                    "choices": plan.choices,
                    "resulting_revision_etag": plan.resulting_revision_etag,
                    "reviewed_by": plan.reviewed_by,
                    "reviewed_at": plan.reviewed_at,
                }
            )
            if normalized != plan:
                raise CatalogError(
                    "modeling plan identifier collision",
                    code="MODELING_PLAN_CONFLICT",
                ) from exc
            return persisted

    def get_modeling_plan(self, plan_id: str) -> ModelingPlan:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(modeling_plans.c.payload).where(modeling_plans.c.id == plan_id)
            ).scalar_one_or_none()
        if payload is None:
            raise CatalogError("modeling plan was not found", code="MODELING_PLAN_NOT_FOUND")
        return ModelingPlan.model_validate(payload)

    def get_latest_modeling_plan(
        self,
        *,
        project_id: str,
        revision_id: str | None = None,
    ) -> ModelingPlan | None:
        statement = select(modeling_plans.c.payload).where(
            modeling_plans.c.project_id == project_id
        )
        if revision_id is not None:
            statement = statement.where(modeling_plans.c.revision_id == revision_id)
        statement = statement.order_by(modeling_plans.c.created_at.desc()).limit(1)
        with self._engine.connect() as connection:
            payload = connection.execute(statement).scalar_one_or_none()
        return ModelingPlan.model_validate(payload) if payload is not None else None

    def apply_modeling_plan_review(
        self,
        *,
        revision: ModelingRevision,
        previous_etag: int,
        plan: ModelingPlan,
        run: ModelingSuggestionRun | None = None,
    ) -> None:
        """Atomically persist the reviewed plan and every resulting state change."""

        if (
            plan.status is not ModelingPlanStatus.APPLIED
            or plan.revision_id != revision.id
            or plan.project_id != revision.project_id
            or plan.revision_etag != previous_etag
            or plan.resulting_revision_etag != revision.etag
        ):
            raise CatalogError(
                "modeling plan review does not match the resulting revision",
                code="MODELING_PLAN_REVIEW_INVALID",
            )
        if run is not None and (
            run.status is not ModelingRunStatus.APPLIED
            or run.revision_id != revision.id
            or run.project_id != revision.project_id
            or run.revision_etag != previous_etag
            or run.resulting_revision_etag != revision.etag
        ):
            raise CatalogError(
                "modeling review does not match the resulting revision",
                code="MODELING_REVIEW_INVALID",
            )

        with self._engine.begin() as connection:
            revision_result = connection.execute(
                update(revisions)
                .where(revisions.c.id == revision.id, revisions.c.etag == previous_etag)
                .values(
                    etag=revision.etag,
                    state=revision.state.value,
                    spec_hash=revision.semantic_spec.spec_hash,
                    payload=revision.model_dump(mode="json"),
                    updated_at=datetime.now(UTC),
                )
            )
            if revision_result.rowcount != 1:
                raise CatalogError(
                    "revision was concurrently modified",
                    code="REVISION_ETAG_CONFLICT",
                )

            if run is not None:
                run_result = connection.execute(
                    update(modeling_runs)
                    .where(
                        modeling_runs.c.id == run.id,
                        modeling_runs.c.revision_id == revision.id,
                        modeling_runs.c.revision_etag == previous_etag,
                        modeling_runs.c.status == ModelingRunStatus.COMPLETED.value,
                    )
                    .values(
                        status=run.status.value,
                        payload=run.model_dump(mode="json"),
                    )
                )
                if run_result.rowcount != 1:
                    raise CatalogError(
                        "modeling review was concurrently modified",
                        code="REVISION_ETAG_CONFLICT",
                    )

            plan_result = connection.execute(
                update(modeling_plans)
                .where(
                    modeling_plans.c.id == plan.id,
                    modeling_plans.c.project_id == revision.project_id,
                    modeling_plans.c.revision_id == revision.id,
                    modeling_plans.c.status == ModelingPlanStatus.READY.value,
                    modeling_plans.c.revision_etag == previous_etag,
                )
                .values(
                    status=plan.status.value,
                    payload=plan.model_dump(mode="json"),
                )
            )
            if plan_result.rowcount != 1:
                raise CatalogError(
                    "modeling plan was concurrently reviewed",
                    code="MODELING_PLAN_CONFLICT",
                )

    def apply_modeling_run_review(
        self,
        *,
        revision: ModelingRevision,
        previous_etag: int,
        run: ModelingSuggestionRun,
    ) -> None:
        """Atomically persist the human review audit and resulting revision."""

        if (
            run.status is not ModelingRunStatus.APPLIED
            or run.revision_id != revision.id
            or run.project_id != revision.project_id
            or run.resulting_revision_etag != revision.etag
        ):
            raise CatalogError(
                "modeling review does not match the resulting revision",
                code="MODELING_REVIEW_INVALID",
            )
        with self._engine.begin() as connection:
            revision_result = connection.execute(
                update(revisions)
                .where(revisions.c.id == revision.id, revisions.c.etag == previous_etag)
                .values(
                    etag=revision.etag,
                    state=revision.state.value,
                    spec_hash=revision.semantic_spec.spec_hash,
                    payload=revision.model_dump(mode="json"),
                    updated_at=datetime.now(UTC),
                )
            )
            run_result = connection.execute(
                update(modeling_runs)
                .where(
                    modeling_runs.c.id == run.id,
                    modeling_runs.c.revision_id == revision.id,
                    modeling_runs.c.revision_etag == previous_etag,
                    modeling_runs.c.status == ModelingRunStatus.COMPLETED.value,
                )
                .values(
                    status=run.status.value,
                    payload=run.model_dump(mode="json"),
                )
            )
            if revision_result.rowcount != 1 or run_result.rowcount != 1:
                raise CatalogError(
                    "modeling review was concurrently modified",
                    code="REVISION_ETAG_CONFLICT",
                )

    def apply_modeling_proposal_review(
        self,
        *,
        revision: ModelingRevision,
        previous_revision_etag: int,
        run: ModelingSuggestionRun,
        proposal: ModelingProposal,
        previous_proposal_etag: int,
        previous_proposal_hash: str,
    ) -> None:
        """Atomically commit Revision, AI-run audit and final proposal audit."""

        if (
            run.status is not ModelingRunStatus.APPLIED
            or proposal.status is not ModelingProposalStatus.APPLIED
            or proposal.suggestion_run_id != run.id
            or run.revision_id != revision.id
            or proposal.revision_id != revision.id
            or run.resulting_revision_etag != revision.etag
            or proposal.resulting_revision_etag != revision.etag
        ):
            raise CatalogError(
                "modeling proposal review does not match the resulting revision",
                code="MODELING_PROPOSAL_REVIEW_INVALID",
            )
        with self._engine.begin() as connection:
            revision_result = connection.execute(
                update(revisions)
                .where(
                    revisions.c.id == revision.id,
                    revisions.c.etag == previous_revision_etag,
                )
                .values(
                    etag=revision.etag,
                    state=revision.state.value,
                    spec_hash=revision.semantic_spec.spec_hash,
                    payload=revision.model_dump(mode="json"),
                    updated_at=datetime.now(UTC),
                )
            )
            run_result = connection.execute(
                update(modeling_runs)
                .where(
                    modeling_runs.c.id == run.id,
                    modeling_runs.c.revision_id == revision.id,
                    modeling_runs.c.revision_etag == previous_revision_etag,
                    modeling_runs.c.status == ModelingRunStatus.COMPLETED.value,
                )
                .values(status=run.status.value, payload=run.model_dump(mode="json"))
            )
            proposal_result = connection.execute(
                update(modeling_proposals)
                .where(
                    modeling_proposals.c.id == proposal.id,
                    modeling_proposals.c.revision_id == revision.id,
                    modeling_proposals.c.etag == previous_proposal_etag,
                    modeling_proposals.c.proposal_hash == previous_proposal_hash,
                    modeling_proposals.c.status == ModelingProposalStatus.DRAFT.value,
                )
                .values(
                    etag=proposal.etag,
                    status=proposal.status.value,
                    proposal_hash=proposal.proposal_hash,
                    payload=proposal.model_dump(mode="json"),
                    updated_at=proposal.updated_at,
                )
            )
            if (
                revision_result.rowcount != 1
                or run_result.rowcount != 1
                or proposal_result.rowcount != 1
            ):
                raise CatalogError(
                    "modeling proposal review was concurrently modified",
                    code="MODELING_PROPOSAL_CONFLICT",
                )

    def save_dimension_dictionary_preview(self, preview: DimensionDictionaryPreview) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(dimension_dictionary_previews).values(
                        id=preview.id,
                        project_id=preview.project_id,
                        revision_id=preview.revision_id,
                        revision_etag=preview.revision_etag,
                        status=preview.status.value,
                        semantic_spec_hash=preview.semantic_spec_hash,
                        payload=preview.model_dump(mode="json"),
                        created_at=preview.created_at,
                    )
                )
            return
        except IntegrityError:
            pass
        persisted = self.get_dimension_dictionary_preview(preview.id)
        if persisted.model_copy(update={"created_at": preview.created_at}) != preview:
            raise CatalogError(
                "dimension dictionary preview identifier collision",
                code="DIMENSION_DICTIONARY_PREVIEW_CONFLICT",
            )

    def get_dimension_dictionary_preview(self, preview_id: str) -> DimensionDictionaryPreview:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(dimension_dictionary_previews.c.payload).where(
                    dimension_dictionary_previews.c.id == preview_id
                )
            ).scalar_one_or_none()
        if payload is None:
            raise CatalogError(
                "dimension dictionary preview was not found",
                code="DIMENSION_DICTIONARY_PREVIEW_NOT_FOUND",
            )
        return DimensionDictionaryPreview.model_validate(payload)

    def list_dimension_dictionary_previews(
        self,
        *,
        project_id: str,
    ) -> tuple[DimensionDictionaryPreview, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(dimension_dictionary_previews.c.payload)
                .where(dimension_dictionary_previews.c.project_id == project_id)
                .order_by(dimension_dictionary_previews.c.created_at)
            ).scalars()
            return tuple(DimensionDictionaryPreview.model_validate(payload) for payload in payloads)

    def apply_dimension_dictionary_review(
        self,
        *,
        revision: ModelingRevision,
        previous_etag: int,
        preview: DimensionDictionaryPreview,
    ) -> None:
        """Atomically bind a complete value review to its resulting revision."""

        if (
            preview.status is not DimensionDictionaryStatus.APPLIED
            or preview.revision_id != revision.id
            or preview.project_id != revision.project_id
            or preview.revision_etag != previous_etag
            or preview.resulting_revision_etag != revision.etag
        ):
            raise CatalogError(
                "dimension dictionary review does not match the resulting revision",
                code="DIMENSION_DICTIONARY_REVIEW_INVALID",
            )
        with self._engine.begin() as connection:
            revision_result = connection.execute(
                update(revisions)
                .where(revisions.c.id == revision.id, revisions.c.etag == previous_etag)
                .values(
                    etag=revision.etag,
                    state=revision.state.value,
                    spec_hash=revision.semantic_spec.spec_hash,
                    payload=revision.model_dump(mode="json"),
                    updated_at=datetime.now(UTC),
                )
            )
            preview_result = connection.execute(
                update(dimension_dictionary_previews)
                .where(
                    dimension_dictionary_previews.c.id == preview.id,
                    dimension_dictionary_previews.c.revision_id == revision.id,
                    dimension_dictionary_previews.c.revision_etag == previous_etag,
                    dimension_dictionary_previews.c.status
                    == DimensionDictionaryStatus.COMPLETED.value,
                )
                .values(
                    status=preview.status.value,
                    payload=preview.model_dump(mode="json"),
                )
            )
            if revision_result.rowcount != 1 or preview_result.rowcount != 1:
                raise CatalogError(
                    "dimension dictionary review was concurrently modified",
                    code="REVISION_ETAG_CONFLICT",
                )

    def save_index_snapshot(
        self,
        *,
        project_id: str,
        index_snapshot: SemanticIndexSnapshot,
    ) -> None:
        """Persist the exact semantic index used by evaluation for later publication."""

        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(index_snapshots).values(
                        id=index_snapshot.id,
                        project_id=project_id,
                        release_spec_hash=index_snapshot.release_spec_hash,
                        content_hash=index_snapshot.content_hash,
                        state=index_snapshot.state.value,
                        payload=index_snapshot.model_dump(mode="json"),
                        created_at=datetime.now(UTC),
                    )
                )
            return
        except IntegrityError:
            pass
        with self._engine.connect() as connection:
            existing = (
                connection.execute(
                    select(
                        index_snapshots.c.project_id,
                        index_snapshots.c.payload,
                    ).where(index_snapshots.c.id == index_snapshot.id)
                )
                .mappings()
                .one_or_none()
            )
        if existing is None:
            raise CatalogError(
                "semantic index could not be persisted",
                code="INDEX_SNAPSHOT_WRITE_FAILED",
            )
        persisted = SemanticIndexSnapshot.model_validate(existing["payload"])
        # Compare on the same basis the id is derived from.  Embedding vectors
        # are outside that basis on purpose (see ``index_identity_hash``), so a
        # rebuild that returns float-noise-different vectors for identical
        # semantics reuses the stored snapshot instead of raising a collision.
        if (
            existing["project_id"] != project_id
            or persisted.content_hash != index_snapshot.content_hash
        ):
            raise CatalogError(
                "semantic index identifier collision",
                code="INDEX_SNAPSHOT_CONFLICT",
            )

    def get_index_snapshot(
        self,
        index_snapshot_id: str,
        *,
        project_id: str | None = None,
    ) -> SemanticIndexSnapshot:
        with self._engine.connect() as connection:
            query = select(
                index_snapshots.c.project_id,
                index_snapshots.c.payload,
            ).where(index_snapshots.c.id == index_snapshot_id)
            row = connection.execute(query).mappings().one_or_none()
        if row is None or (project_id is not None and row["project_id"] != project_id):
            raise CatalogError("semantic index was not found", code="INDEX_SNAPSHOT_NOT_FOUND")
        return SemanticIndexSnapshot.model_validate(row["payload"])

    def publish(
        self,
        *,
        revision: ModelingRevision,
        index_snapshot: SemanticIndexSnapshot,
    ) -> PublishedRelease:
        if revision.state is not RevisionState.VALIDATED:
            raise CatalogError("only a validated revision can be published", code="NOT_VALIDATED")
        if index_snapshot.state is not IndexState.READY:
            raise CatalogError("semantic index is not ready", code="INDEX_NOT_READY")
        if revision.semantic_spec.spec_hash != index_snapshot.release_spec_hash:
            raise CatalogError("release and index hashes differ", code="RELEASE_INDEX_MISMATCH")
        release_id = f"rel_{uuid.uuid4().hex}"
        release = revision.semantic_spec.model_copy(
            update={
                "id": release_id,
                "revision_id": revision.id,
                "index_snapshot_id": index_snapshot.id,
            }
        )
        published_revision = revision.model_copy(update={"state": RevisionState.PUBLISHED})
        now = datetime.now(UTC)
        with self._engine.begin() as connection:
            project_row = connection.execute(
                select(projects.c.id).where(projects.c.id == revision.project_id).with_for_update()
            ).one_or_none()
            if project_row is None:
                raise CatalogError("analytics project was not found", code="PROJECT_NOT_FOUND")
            current = connection.execute(
                select(revisions.c.etag, revisions.c.state)
                .where(revisions.c.id == revision.id)
                .with_for_update()
            ).one_or_none()
            if (
                current is None
                or current.etag != revision.etag
                or current.state != RevisionState.VALIDATED.value
            ):
                raise CatalogError("revision changed before publish", code="REVISION_ETAG_CONFLICT")
            persisted_index = (
                connection.execute(
                    select(
                        index_snapshots.c.project_id,
                        index_snapshots.c.payload,
                    ).where(index_snapshots.c.id == index_snapshot.id)
                )
                .mappings()
                .one_or_none()
            )
            if persisted_index is None:
                connection.execute(
                    insert(index_snapshots).values(
                        id=index_snapshot.id,
                        project_id=revision.project_id,
                        release_spec_hash=index_snapshot.release_spec_hash,
                        content_hash=index_snapshot.content_hash,
                        state=index_snapshot.state.value,
                        payload=index_snapshot.model_dump(mode="json"),
                        created_at=now,
                    )
                )
            else:
                persisted = SemanticIndexSnapshot.model_validate(persisted_index["payload"])
                if (
                    persisted_index["project_id"] != revision.project_id
                    or persisted.content_hash != index_snapshot.content_hash
                ):
                    raise CatalogError(
                        "semantic index identifier collision",
                        code="INDEX_SNAPSHOT_CONFLICT",
                    )
            connection.execute(
                update(releases)
                .where(
                    releases.c.project_id == revision.project_id,
                    releases.c.status == "active",
                )
                .values(status="retired")
            )
            connection.execute(
                insert(releases).values(
                    id=release.id,
                    project_id=revision.project_id,
                    revision_id=revision.id,
                    spec_hash=release.spec_hash,
                    index_snapshot_id=index_snapshot.id,
                    status="active",
                    payload=release.model_dump(mode="json"),
                    created_at=now,
                )
            )
            revision_update = connection.execute(
                update(revisions)
                .where(
                    revisions.c.id == revision.id,
                    revisions.c.etag == revision.etag,
                    revisions.c.state == RevisionState.VALIDATED.value,
                )
                .values(
                    state=RevisionState.PUBLISHED.value,
                    payload=published_revision.model_dump(mode="json"),
                    updated_at=now,
                )
            )
            if revision_update.rowcount != 1:
                raise CatalogError(
                    "revision changed before publish",
                    code="REVISION_ETAG_CONFLICT",
                )
            connection.execute(
                update(projects)
                .where(projects.c.id == revision.project_id)
                .values(active_release_id=release.id)
            )
            governance_payload = connection.execute(
                select(domain_governance.c.payload)
                .where(domain_governance.c.project_id == revision.project_id)
                .with_for_update()
            ).scalar_one_or_none()
            if governance_payload is not None:
                governance = DomainGovernance.model_validate(governance_payload)
                online = governance.model_copy(
                    update={
                        "etag": governance.etag + 1,
                        "lifecycle": DomainLifecycle.ONLINE,
                        "updated_by": "release-publisher",
                        "updated_at": now,
                    }
                )
                governance_update = connection.execute(
                    update(domain_governance)
                    .where(
                        domain_governance.c.project_id == revision.project_id,
                        domain_governance.c.etag == governance.etag,
                    )
                    .values(
                        etag=online.etag,
                        lifecycle=online.lifecycle.value,
                        payload=online.model_dump(mode="json"),
                        updated_at=now,
                    )
                )
                if governance_update.rowcount != 1:
                    raise CatalogError(
                        "domain governance changed before publish",
                        code="DOMAIN_ETAG_CONFLICT",
                    )
        return PublishedRelease(release=release, index_snapshot=index_snapshot, status="active")

    def rollback_active_release(self, *, project_id: str) -> str:
        """Point the project back at the release published before the active one.

        A wrong metric definition that reached production previously had no fast
        exit: the only path was building a new candidate, re-running the quality
        scan and the 30-case evaluation, and publishing again, while production
        kept serving wrong numbers. The published releases are immutable
        snapshots, so switching the pointer is enough.
        """

        with self._engine.begin() as connection:
            active = connection.execute(
                select(projects.c.active_release_id).where(projects.c.id == project_id)
            ).scalar_one_or_none()
            if not active:
                raise CatalogError("project has no active release to roll back")
            active_created_at = connection.execute(
                select(releases.c.created_at).where(releases.c.id == active)
            ).scalar_one_or_none()
            if active_created_at is None:
                raise CatalogError("active release was not found")
            previous = connection.execute(
                select(releases.c.id)
                .where(releases.c.project_id == project_id)
                .where(releases.c.created_at < active_created_at)
                .order_by(releases.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if previous is None:
                raise CatalogError("no earlier release is available to roll back to")
            # publish 维护「同一项目下只有一条 status=active」的不变量，回滚
            # 必须同样维护它：只改指针会让线上版本自称 retired，而被回滚掉的
            # 版本仍标着 active。
            connection.execute(
                update(releases)
                .where(releases.c.project_id == project_id)
                .where(releases.c.status == "active")
                .values(status="retired")
            )
            connection.execute(
                update(releases).where(releases.c.id == previous).values(status="active")
            )
            connection.execute(
                update(projects)
                .where(projects.c.id == project_id)
                .values(active_release_id=previous)
            )
        return previous

    def list_releases(self, *, project_id: str, limit: int = 50) -> tuple[ReleaseSummary, ...]:
        """发布历史，最新在前。能发布却看不到发过什么、不能回滚，此前是盲的。"""

        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        releases.c.id,
                        releases.c.revision_id,
                        releases.c.spec_hash,
                        releases.c.status,
                        releases.c.created_at,
                    )
                    .where(releases.c.project_id == project_id)
                    .order_by(releases.c.created_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return tuple(ReleaseSummary(**row) for row in rows)

    def get_release(self, release_id: str) -> PublishedRelease:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        releases.c.payload,
                        releases.c.status,
                        index_snapshots.c.payload.label("index_payload"),
                    )
                    .join(
                        index_snapshots,
                        releases.c.index_snapshot_id == index_snapshots.c.id,
                    )
                    .where(releases.c.id == release_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise CatalogError("semantic release was not found", code="RELEASE_NOT_FOUND")
        release = SemanticRelease.model_validate(row["payload"])
        index = SemanticIndexSnapshot.model_validate(row["index_payload"])
        if (
            release.index_snapshot_id != index.id
            or release.spec_hash != index.release_spec_hash
            or index.state is not IndexState.READY
        ):
            raise CatalogError("release snapshot is inconsistent", code="RELEASE_INDEX_MISMATCH")
        return PublishedRelease(release=release, index_snapshot=index, status=row["status"])

    def get_active_release(self, project_id: str) -> PublishedRelease:
        project = self.get_project(project_id)
        if project.active_release_id is None:
            raise CatalogError("project has no active release", code="ACTIVE_RELEASE_NOT_FOUND")
        if self.get_domain_governance(project_id).lifecycle is not DomainLifecycle.ONLINE:
            raise CatalogError("analytics domain is not online", code="DOMAIN_OFFLINE")
        return self.get_release(project.active_release_id)

    def save_evaluation_report(self, report: EvaluationReport) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(evaluation_reports).values(
                    id=report.id,
                    project_id=report.project_id,
                    spec_hash=report.spec_hash,
                    suite_id=report.suite_id,
                    gate_passed="true" if report.gate_passed else "false",
                    payload=report.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )

    def save_golden_suite(self, record: GoldenSuiteRecord) -> None:
        payload = record.model_dump(mode="json")
        values = {
            "project_id": record.project_id,
            "revision_id": record.revision_id,
            "revision_etag": record.revision_etag,
            "semantic_spec_hash": record.semantic_spec_hash,
            "payload": payload,
            "updated_at": record.updated_at,
        }

        def require_same_scope(existing) -> None:
            if (
                existing["project_id"] != record.project_id
                or existing["revision_id"] != record.revision_id
            ):
                raise CatalogError(
                    "golden suite identifier belongs to another modeling scope",
                    code="GOLDEN_SUITE_SCOPE_CONFLICT",
                )

        try:
            with self._engine.begin() as connection:
                existing = (
                    connection.execute(
                        select(
                            golden_suites.c.project_id,
                            golden_suites.c.revision_id,
                        )
                        .where(golden_suites.c.id == record.id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    connection.execute(insert(golden_suites).values(id=record.id, **values))
                else:
                    require_same_scope(existing)
                    connection.execute(
                        update(golden_suites)
                        .where(
                            golden_suites.c.id == record.id,
                            golden_suites.c.project_id == record.project_id,
                            golden_suites.c.revision_id == record.revision_id,
                        )
                        .values(**values)
                    )
            return
        except IntegrityError:
            # Two writers can both observe an absent identifier. Resolve the
            # insert race without ever allowing the winning row to change scope.
            pass

        with self._engine.begin() as connection:
            existing = (
                connection.execute(
                    select(
                        golden_suites.c.project_id,
                        golden_suites.c.revision_id,
                    )
                    .where(golden_suites.c.id == record.id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise CatalogError(
                    "golden suite could not be persisted",
                    code="GOLDEN_SUITE_WRITE_FAILED",
                )
            require_same_scope(existing)
            connection.execute(
                update(golden_suites)
                .where(
                    golden_suites.c.id == record.id,
                    golden_suites.c.project_id == record.project_id,
                    golden_suites.c.revision_id == record.revision_id,
                )
                .values(**values)
            )

    def list_golden_suites(
        self,
        *,
        project_id: str,
        revision_id: str,
    ) -> tuple[GoldenSuiteRecord, ...]:
        from knowflow_analytics.evaluation.contracts import GoldenSuiteRecord

        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(golden_suites.c.payload)
                .where(golden_suites.c.project_id == project_id)
                .where(golden_suites.c.revision_id == revision_id)
                .order_by(golden_suites.c.updated_at.desc())
            ).scalars()
            return tuple(GoldenSuiteRecord.model_validate(payload) for payload in payloads)

    def delete_golden_suite(
        self,
        *,
        suite_id: str,
        project_id: str,
        revision_id: str,
    ) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                delete(golden_suites)
                .where(golden_suites.c.id == suite_id)
                .where(golden_suites.c.project_id == project_id)
                .where(golden_suites.c.revision_id == revision_id)
            )
            return bool(result.rowcount)

    def get_latest_evaluation(self, spec_hash: str) -> EvaluationReport | None:
        from knowflow_analytics.evaluation.contracts import EvaluationReport

        with self._engine.connect() as connection:
            payload = connection.execute(
                select(evaluation_reports.c.payload)
                .where(evaluation_reports.c.spec_hash == spec_hash)
                .order_by(evaluation_reports.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        return EvaluationReport.model_validate(payload) if payload is not None else None

    def save_success(
        self,
        turn,
        *,
        actor_id: str,
        project_id: str,
        conversation_id: str,
    ) -> None:
        """Persist one successful logical query turn for multi-turn rewrite."""

        from knowflow_analytics.query.multi_turn import QueryHistoryTurn

        validated = QueryHistoryTurn.model_validate(turn)
        with self._engine.begin() as connection:
            connection.execute(
                insert(query_history).values(
                    actor_id=actor_id,
                    project_id=project_id,
                    conversation_id=conversation_id,
                    release_id=validated.release_id,
                    spec_hash=validated.spec_hash,
                    index_snapshot_id=validated.index_snapshot_id,
                    dataset_id=validated.dataset_id,
                    payload=validated.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )

    def save_failure(
        self,
        record,
        *,
        actor_id: str,
        project_id: str,
    ) -> None:
        """Append one refused question for offline vocabulary mining."""

        from knowflow_analytics.query.contracts import QueryFailureRecord

        validated = QueryFailureRecord.model_validate(record)
        with self._engine.begin() as connection:
            connection.execute(
                insert(query_failures).values(
                    actor_id=actor_id,
                    project_id=project_id,
                    release_id=validated.release_id,
                    spec_hash=validated.spec_hash,
                    index_snapshot_id=validated.index_snapshot_id,
                    stage=validated.stage,
                    code=validated.code,
                    payload=validated.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )

    def list_failures(self, *, project_id: str, limit: int = 500) -> tuple:
        """Newest first. Read side for the mining job; not used online."""

        from knowflow_analytics.query.contracts import QueryFailureRecord

        with self._engine.connect() as connection:
            rows = connection.execute(
                select(query_failures.c.payload)
                .where(query_failures.c.project_id == project_id)
                .order_by(query_failures.c.id.desc())
                .limit(limit)
            ).all()
        return tuple(QueryFailureRecord.model_validate(row.payload) for row in rows)

    def save_confirmation_memory(self, memory) -> None:
        from knowflow_analytics.query.confirmation_memory import ConfirmationMemory

        validated = ConfirmationMemory.model_validate(memory)
        with self._engine.begin() as connection:
            # The same actor/project lock used by diagnostic retention also
            # makes memory UPSERT + conflict/cap handling atomic on PostgreSQL.
            self._acquire_query_diagnostic_scope_lock(
                connection,
                actor_id=validated.actor_id,
                project_id=validated.project_id,
            )
            connection.execute(
                delete(confirmation_memories)
                .where(confirmation_memories.c.actor_id == validated.actor_id)
                .where(confirmation_memories.c.project_id == validated.project_id)
                .where(confirmation_memories.c.expires_at <= validated.created_at)
            )
            existing_row = connection.execute(
                select(
                    confirmation_memories.c.payload,
                    confirmation_memories.c.revoked_at,
                )
                .where(confirmation_memories.c.id == validated.id)
                .where(confirmation_memories.c.actor_id == validated.actor_id)
                .where(confirmation_memories.c.project_id == validated.project_id)
            ).first()
            if existing_row is not None:
                if existing_row.revoked_at is not None:
                    return
                existing = ConfirmationMemory.model_validate(existing_row.payload)
                refreshed = validated.model_copy(update={"created_at": existing.created_at})
                connection.execute(
                    update(confirmation_memories)
                    .where(confirmation_memories.c.id == validated.id)
                    .values(
                        payload=refreshed.model_dump(mode="json"),
                        expires_at=refreshed.expires_at,
                    )
                )
                return
            connection.execute(
                insert(confirmation_memories).values(
                    id=validated.id,
                    actor_id=validated.actor_id,
                    project_id=validated.project_id,
                    release_id=validated.release_id,
                    spec_hash=validated.spec_hash,
                    index_snapshot_id=validated.index_snapshot_id,
                    normalized_phrase=validated.normalized_phrase,
                    candidate_set_hash=validated.candidate_set_hash,
                    exact_context_hash=validated.exact_context_hash,
                    payload=validated.model_dump(mode="json"),
                    created_at=validated.created_at,
                    expires_at=validated.expires_at,
                    revoked_at=validated.revoked_at,
                )
            )
            overflow_rows = (
                connection.execute(
                    select(confirmation_memories.c.payload)
                    .where(confirmation_memories.c.actor_id == validated.actor_id)
                    .where(confirmation_memories.c.project_id == validated.project_id)
                    .order_by(confirmation_memories.c.created_at.desc())
                    .offset(500)
                    .limit(100)
                )
                .scalars()
                .all()
            )
            overflow_bindings = {
                (
                    memory.release_id,
                    memory.spec_hash,
                    memory.index_snapshot_id,
                    memory.normalized_phrase,
                    memory.candidate_set_hash,
                    memory.exact_context_hash,
                )
                for payload in overflow_rows
                for memory in (ConfirmationMemory.model_validate(payload),)
            }
            for (
                release_id,
                spec_hash,
                index_snapshot_id,
                normalized_phrase,
                candidate_set_hash,
                exact_context_hash,
            ) in overflow_bindings:
                connection.execute(
                    delete(confirmation_memories)
                    .where(confirmation_memories.c.actor_id == validated.actor_id)
                    .where(confirmation_memories.c.project_id == validated.project_id)
                    .where(confirmation_memories.c.release_id == release_id)
                    .where(confirmation_memories.c.spec_hash == spec_hash)
                    .where(confirmation_memories.c.index_snapshot_id == index_snapshot_id)
                    .where(confirmation_memories.c.normalized_phrase == normalized_phrase)
                    .where(confirmation_memories.c.candidate_set_hash == candidate_set_hash)
                    .where(confirmation_memories.c.exact_context_hash == exact_context_hash)
                )

    def find_confirmation_memory(
        self,
        *,
        actor_id: str,
        project_id: str,
        release_id: str,
        spec_hash: str,
        index_snapshot_id: str,
        normalized_phrase: str,
        candidate_set_hash: str,
        exact_context_hash: str,
        now: datetime,
    ):
        from knowflow_analytics.query.confirmation_memory import ConfirmationMemory

        with self._engine.connect() as connection:
            rows = connection.execute(
                select(confirmation_memories.c.payload)
                .where(confirmation_memories.c.actor_id == actor_id)
                .where(confirmation_memories.c.project_id == project_id)
                .where(confirmation_memories.c.release_id == release_id)
                .where(confirmation_memories.c.spec_hash == spec_hash)
                .where(confirmation_memories.c.index_snapshot_id == index_snapshot_id)
                .where(confirmation_memories.c.normalized_phrase == normalized_phrase)
                .where(confirmation_memories.c.candidate_set_hash == candidate_set_hash)
                .where(confirmation_memories.c.exact_context_hash == exact_context_hash)
                .where(confirmation_memories.c.revoked_at.is_(None))
                .where(confirmation_memories.c.expires_at > now)
                .order_by(confirmation_memories.c.created_at.desc())
                .limit(3)
            ).all()
        memories = tuple(ConfirmationMemory.model_validate(row.payload) for row in rows)
        selections = {
            (
                item.selection_kind,
                item.semantic_element_id,
                item.dataset_id,
            )
            for item in memories
        }
        return memories[0] if memories and len(selections) == 1 else None

    def list_confirmation_memories(
        self,
        *,
        actor_id: str,
        project_id: str,
        include_revoked: bool = False,
    ) -> tuple:
        from knowflow_analytics.query.confirmation_memory import ConfirmationMemory

        statement = (
            select(confirmation_memories.c.payload)
            .where(confirmation_memories.c.actor_id == actor_id)
            .where(confirmation_memories.c.project_id == project_id)
            .order_by(confirmation_memories.c.created_at.desc())
            .limit(500)
        )
        if not include_revoked:
            statement = statement.where(confirmation_memories.c.revoked_at.is_(None))
        statement = statement.where(confirmation_memories.c.expires_at > datetime.now(UTC))
        with self._engine.connect() as connection:
            rows = connection.execute(statement).all()
        return tuple(ConfirmationMemory.model_validate(row.payload) for row in rows)

    def revoke_confirmation_memory(
        self,
        *,
        memory_id: str,
        actor_id: str,
        project_id: str,
        revoked_at: datetime,
    ) -> bool:
        from knowflow_analytics.query.confirmation_memory import ConfirmationMemory

        with self._engine.begin() as connection:
            row = connection.execute(
                select(confirmation_memories.c.payload)
                .where(confirmation_memories.c.id == memory_id)
                .where(confirmation_memories.c.actor_id == actor_id)
                .where(confirmation_memories.c.project_id == project_id)
                .where(confirmation_memories.c.revoked_at.is_(None))
            ).first()
            if row is None:
                return False
            memory = ConfirmationMemory.model_validate(row.payload).model_copy(
                update={"revoked_at": revoked_at}
            )
            connection.execute(
                update(confirmation_memories)
                .where(confirmation_memories.c.id == memory_id)
                .values(
                    revoked_at=revoked_at,
                    payload=memory.model_dump(mode="json"),
                )
            )
        return True

    def save_query_diagnostic(self, artifact) -> None:
        """Append one bounded diagnostic without retaining recoverable secrets."""

        from knowflow_analytics.query.diagnostics import (
            QUERY_DIAGNOSTIC_MAX_ARTIFACT_BYTES,
            QUERY_DIAGNOSTIC_MAX_PER_ACTOR_PROJECT,
            QUERY_DIAGNOSTIC_PURGE_BATCH_SIZE,
            QueryDiagnosticArtifact,
            permanently_redact_artifact,
        )

        validated = permanently_redact_artifact(QueryDiagnosticArtifact.model_validate(artifact))
        payload = validated.model_dump(mode="json")
        encoded_size = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        )
        if encoded_size > QUERY_DIAGNOSTIC_MAX_ARTIFACT_BYTES:
            raise CatalogError(
                "query diagnostic exceeds its storage limit",
                code="QUERY_DIAGNOSTIC_TOO_LARGE",
            )
        with self._engine.begin() as connection:
            self._acquire_query_diagnostic_scope_lock(
                connection,
                actor_id=validated.actor_id,
                project_id=validated.project_id,
            )
            now_monotonic = time.monotonic()
            should_purge = False
            with self._query_diagnostic_purge_lock:
                if now_monotonic - self._query_diagnostic_last_purge >= 60:
                    self._query_diagnostic_last_purge = now_monotonic
                    should_purge = True
            if should_purge:
                self._purge_expired_query_diagnostics_with_connection(
                    connection,
                    now=datetime.now(UTC),
                    batch_size=QUERY_DIAGNOSTIC_PURGE_BATCH_SIZE,
                )
            connection.execute(
                insert(query_diagnostics).values(
                    query_id=validated.query_id,
                    actor_id=validated.actor_id,
                    project_id=validated.project_id,
                    permission_scope_hash=validated.permission_scope_hash,
                    expires_at=validated.expires_at,
                    payload=payload,
                    created_at=validated.created_at,
                )
            )
            # Keep the newest bounded history for this actor/project. Scope
            # hashes remain authorization keys, while quota accounting spans
            # their rotations so a caller cannot multiply retention by minting
            # new permission snapshots.
            while True:
                overflow_ids = (
                    connection.execute(
                        select(query_diagnostics.c.id)
                        .where(query_diagnostics.c.actor_id == validated.actor_id)
                        .where(query_diagnostics.c.project_id == validated.project_id)
                        .order_by(query_diagnostics.c.id.desc())
                        .offset(QUERY_DIAGNOSTIC_MAX_PER_ACTOR_PROJECT)
                        .limit(QUERY_DIAGNOSTIC_PURGE_BATCH_SIZE)
                    )
                    .scalars()
                    .all()
                )
                if not overflow_ids:
                    break
                connection.execute(
                    delete(query_diagnostics).where(query_diagnostics.c.id.in_(overflow_ids))
                )

    @staticmethod
    def _acquire_query_diagnostic_scope_lock(
        connection,
        *,
        actor_id: str,
        project_id: str,
    ) -> None:
        if connection.dialect.name != "postgresql":
            return
        connection.execute(
            select(
                func.pg_advisory_xact_lock(_query_diagnostic_advisory_lock_id(actor_id, project_id))
            )
        )

    def purge_expired_query_diagnostics(
        self,
        *,
        now: datetime | None = None,
        batch_size: int = 100,
    ) -> int:
        """Delete at most one bounded expiry batch and return its size."""

        if not 1 <= batch_size <= 1_000:
            raise ValueError("query diagnostic purge batch size is invalid")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self._engine.begin() as connection:
            return self._purge_expired_query_diagnostics_with_connection(
                connection,
                now=timestamp,
                batch_size=batch_size,
            )

    @staticmethod
    def _purge_expired_query_diagnostics_with_connection(
        connection,
        *,
        now: datetime,
        batch_size: int,
    ) -> int:
        expired_ids = (
            connection.execute(
                select(query_diagnostics.c.id)
                .where(query_diagnostics.c.expires_at <= now)
                .order_by(query_diagnostics.c.id)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )
        if not expired_ids:
            return 0
        connection.execute(delete(query_diagnostics).where(query_diagnostics.c.id.in_(expired_ids)))
        return len(expired_ids)

    def get_query_diagnostic(
        self,
        *,
        actor_id: str,
        project_id: str,
        permission_scope_hash: str,
        query_id: str,
        now: datetime | None = None,
    ):
        """Return only an unexpired artifact in the exact caller scope.

        Every miss intentionally has one error code, so callers cannot probe
        whether a query belongs to a different actor/project or merely expired.
        """

        from knowflow_analytics.query.diagnostics import QueryDiagnosticArtifact

        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(query_diagnostics.c.payload)
                .where(query_diagnostics.c.actor_id == actor_id)
                .where(query_diagnostics.c.project_id == project_id)
                .where(query_diagnostics.c.permission_scope_hash == permission_scope_hash)
                .where(query_diagnostics.c.query_id == query_id)
                .where(query_diagnostics.c.expires_at > timestamp)
                .order_by(query_diagnostics.c.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        if payload is None:
            raise CatalogError(
                "query diagnostic was not found",
                code="QUERY_DIAGNOSTIC_NOT_FOUND",
            )
        return QueryDiagnosticArtifact.model_validate(payload)

    def last_success(
        self,
        *,
        actor_id: str,
        project_id: str,
        conversation_id: str,
        release_id: str,
        spec_hash: str,
        index_snapshot_id: str,
        dataset_id: str,
    ):
        """Return only the latest turn in the exact immutable semantic scope."""

        from knowflow_analytics.query.multi_turn import QueryHistoryTurn

        with self._engine.connect() as connection:
            payload = connection.execute(
                select(query_history.c.payload)
                .where(query_history.c.actor_id == actor_id)
                .where(query_history.c.project_id == project_id)
                .where(query_history.c.conversation_id == conversation_id)
                .where(query_history.c.release_id == release_id)
                .where(query_history.c.spec_hash == spec_hash)
                .where(query_history.c.index_snapshot_id == index_snapshot_id)
                .where(query_history.c.dataset_id == dataset_id)
                .order_by(query_history.c.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        return QueryHistoryTurn.model_validate(payload) if payload is not None else None
