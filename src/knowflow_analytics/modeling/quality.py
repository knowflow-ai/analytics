from __future__ import annotations

import math
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from knowflow_analytics.contracts import (
    Cardinality,
    FieldKind,
    FrozenModel,
    SemanticQuery,
    SemanticRelease,
)
from knowflow_analytics.errors import AnalyticsError, TranslationError
from knowflow_analytics.execution.dialect import SqlDialect, to_dialect_sql
from knowflow_analytics.execution.executor import SqlExecutor, normalize_cell
from knowflow_analytics.hashing import content_hash, semantic_evidence_hash
from knowflow_analytics.modeling.contracts import ModelingRevision
from knowflow_analytics.modeling.source_query import compile_governed_model_source
from knowflow_analytics.semantic.join_planner import JoinPlanner
from knowflow_analytics.semantic.translator import SemanticTranslator

# 样例行只为「人看一眼」，不是导出。超过这个数就该去问数了。
MODEL_ROWS_PREVIEW_LIMIT = 50


class QualityStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    BLOCKING = "blocking"
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ModelingQualityReportStatus(StrEnum):
    COMPLETED = "completed"
    REVIEWED = "reviewed"


class ModelGrainProfile(FrozenModel):
    model_id: str
    identifier_field_ids: tuple[str, ...]
    total_rows: int = Field(ge=0)
    null_rows: int = Field(ge=0)
    distinct_non_null_keys: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)
    uniqueness_rate: float = Field(ge=0.0, le=1.0)
    null_rate: float = Field(ge=0.0, le=1.0)
    status: QualityStatus
    message: str


class RelationDataProfile(FrozenModel):
    relation_id: str
    left_rows: int = Field(ge=0)
    right_rows: int = Field(ge=0)
    matched_left_rows: int = Field(ge=0)
    matched_right_rows: int = Field(ge=0)
    orphan_left_rows: int = Field(ge=0)
    orphan_right_rows: int = Field(ge=0)
    left_join_coverage: float = Field(ge=0.0, le=1.0)
    right_join_coverage: float = Field(ge=0.0, le=1.0)
    max_left_key_multiplicity: int = Field(ge=0)
    max_right_key_multiplicity: int = Field(ge=0)
    joined_rows: int = Field(ge=0)
    left_fanout_factor: float = Field(ge=0.0)
    right_fanout_factor: float = Field(ge=0.0)
    declared_cardinality: Cardinality
    observed_cardinality: Cardinality | None = None
    status: QualityStatus
    message: str


class MetricPreview(FrozenModel):
    id: str
    dataset_id: str
    metric_id: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    status: QualityStatus = QualityStatus.PENDING_REVIEW
    error_code: str | None = None
    message: str = ""
    review_note: str = Field(default="", max_length=2_000)


class ModelRowsPreview(FrozenModel):
    model_id: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    truncated: bool = False


class MetricPreviewDecision(FrozenModel):
    preview_id: str
    confirm: bool
    note: str = Field(default="", max_length=2_000)


class DatasetReachabilityCell(FrozenModel):
    dataset_id: str
    metric_id: str
    dimension_id: str
    metric_model_id: str
    dimension_model_id: str
    relation_ids: tuple[str, ...] = ()
    status: QualityStatus
    reason_code: str
    message: str


class ModelingQualityReport(FrozenModel):
    id: str
    project_id: str
    revision_id: str
    revision_etag: int = Field(ge=1)
    schema_snapshot_hash: str
    semantic_spec_hash: str
    etag: int = Field(ge=1)
    status: ModelingQualityReportStatus = ModelingQualityReportStatus.COMPLETED
    content_hash: str
    ready: bool
    blocking_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    model_grains: tuple[ModelGrainProfile, ...] = ()
    relations: tuple[RelationDataProfile, ...] = ()
    metric_previews: tuple[MetricPreview, ...] = ()
    reachability: tuple[DatasetReachabilityCell, ...] = ()
    created_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    @model_validator(mode="after")
    def review_audit_matches_status(self) -> ModelingQualityReport:
        if self.status is ModelingQualityReportStatus.COMPLETED:
            if self.reviewed_by is not None or self.reviewed_at is not None:
                raise ValueError("completed quality report cannot contain review audit")
        elif self.reviewed_by is None or self.reviewed_at is None:
            raise ValueError("reviewed quality report requires reviewer audit")
        return self


class ModelingQualityError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "MODELING_QUALITY_FAILED") -> None:
        super().__init__(message, code=code, stage="MODELING_QUALITY")


class ModelingQualityProfiler:
    """Collect empirical modeling evidence from a read-only PostgreSQL source.

    Validating the configured resource shapes does not prove grain or
    cardinality empirically, which is what these checks add.  They never change
    catalog resources.
    """

    def __init__(
        self,
        engine: Engine,
        executor: SqlExecutor,
        *,
        statement_timeout_ms: int = 30_000,
        overall_timeout_ms: int = 180_000,
        max_metric_previews: int = 200,
        dialect: SqlDialect = SqlDialect.POSTGRES,
    ) -> None:
        if not 100 <= statement_timeout_ms <= 300_000:
            raise ValueError("statement_timeout_ms must be between 100 and 300000")
        if not 1_000 <= overall_timeout_ms <= 900_000:
            raise ValueError("overall_timeout_ms must be between 1000 and 900000")
        if not 1 <= max_metric_previews <= 2_000:
            raise ValueError("max_metric_previews must be between 1 and 2000")
        self._engine = engine
        self._executor = executor
        self._statement_timeout_ms = statement_timeout_ms
        self._overall_timeout_ms = overall_timeout_ms
        self._max_metric_previews = max_metric_previews
        self._dialect = dialect
        self._translator = SemanticTranslator()

    def with_statement_timeout(self, statement_timeout_ms: int) -> ModelingQualityProfiler:
        """同样的连接与执行器，另一个语句上限。

        发布前那次完整跑给 30 秒，建模页点一下不能这么等——量不完就直说。
        """

        return ModelingQualityProfiler(
            self._engine,
            self._executor,
            statement_timeout_ms=statement_timeout_ms,
            overall_timeout_ms=self._overall_timeout_ms,
            max_metric_previews=self._max_metric_previews,
            dialect=self._dialect,
        )

    def profile(self, revision: ModelingRevision) -> ModelingQualityReport:
        started = time.monotonic()
        release = revision.semantic_spec
        grains = self._profile_grains(release, started=started)
        relations = self._profile_relations(release, started=started)
        previews = self._preview_metrics(release, started=started)
        reachability = self._reachability_matrix(release)
        return _build_report(
            revision=revision,
            model_grains=grains,
            relations=relations,
            metric_previews=previews,
            reachability=reachability,
        )

    def review(
        self,
        report: ModelingQualityReport,
        *,
        decisions: tuple[MetricPreviewDecision, ...],
        reviewed_by: str,
    ) -> ModelingQualityReport:
        decision_by_id = {item.preview_id: item for item in decisions}
        if len(decision_by_id) != len(decisions):
            raise ModelingQualityError(
                "metric preview decisions must be unique",
                code="QUALITY_REVIEW_INVALID",
            )
        reviewable_ids = {
            item.id
            for item in report.metric_previews
            if item.status is QualityStatus.PENDING_REVIEW
        }
        unknown_ids = set(decision_by_id) - reviewable_ids
        if unknown_ids:
            raise ModelingQualityError(
                "only successfully generated metric previews can be reviewed",
                code="QUALITY_REVIEW_INVALID",
            )
        if set(decision_by_id) != reviewable_ids:
            raise ModelingQualityError(
                "every reviewable metric preview must be explicitly confirmed or rejected",
                code="QUALITY_REVIEW_INCOMPLETE",
            )
        previews = tuple(
            (
                item.model_copy(
                    update={
                        "status": (
                            QualityStatus.CONFIRMED
                            if decision_by_id[item.id].confirm
                            else QualityStatus.REJECTED
                        ),
                        "review_note": decision_by_id[item.id].note,
                    }
                )
                if item.id in reviewable_ids
                else item
            )
            for item in report.metric_previews
        )
        now = datetime.now(UTC)
        return _rebuild_report(
            report,
            metric_previews=previews,
            status=ModelingQualityReportStatus.REVIEWED,
            etag=report.etag + 1,
            reviewed_by=reviewed_by,
            reviewed_at=now,
        )

    def _check_deadline(self, started: float) -> None:
        if (time.monotonic() - started) * 1_000 >= self._overall_timeout_ms:
            raise ModelingQualityError(
                "modeling quality profiling exceeded its overall timeout",
                code="MODELING_QUALITY_TIMEOUT",
            )

    def _profile_grains(
        self,
        release: SemanticRelease,
        *,
        started: float,
    ) -> tuple[ModelGrainProfile, ...]:
        profiles: list[ModelGrainProfile] = []
        for model in sorted(release.models, key=lambda item: item.id):
            self._check_deadline(started)
            profiles.append(self.profile_grain(model, release))
        return tuple(profiles)

    def profile_grain(self, model: Any, release: SemanticRelease) -> ModelGrainProfile:
        """用真实数据核对一个模型的事实粒度：主标识是否非空且唯一。

        一次一个模型，发布前的整版报告与建模页的单次核对走同一条语句。
        """

        identifiers = tuple(
            item
            for item in release.fields
            if item.model_id == model.id and item.identifier_type == "primary"
        )
        if not identifiers:
            return ModelGrainProfile(
                model_id=model.id,
                identifier_field_ids=(),
                total_rows=0,
                null_rows=0,
                distinct_non_null_keys=0,
                duplicate_rows=0,
                uniqueness_rate=0.0,
                null_rate=0.0,
                status=QualityStatus.WARNING,
                message=(
                    "模型未配置主标识，无法用数据证明事实粒度。"
                    "这张表仍然可以被问到（默认计数按行统计），但它参与一对多连接时，"
                    "跨表求和会被重复计数放大——那种查询会被拒绝。"
                    "需要跨表聚合就确认一个在数据里唯一的主标识。"
                ),
            )
        source_sql, parameters = self._model_source(model, release)
        # 提示里指名道姓：建模者看到「主标识有 4 万条重复」时得知道该改哪一列
        identifier_label = " + ".join(
            f"{item.name}（{item.column}）" if item.name != item.column else item.column
            for item in identifiers
        )
        columns = [_quote_identifier(item.column) for item in identifiers]
        null_predicate = " OR ".join(f"{item} IS NULL" for item in columns)
        non_null_predicate = " AND ".join(f"{item} IS NOT NULL" for item in columns)
        distinct_expr = columns[0] if len(columns) == 1 else f"({', '.join(columns)})"
        query = text(
            f"""
            WITH source AS ({source_sql})
            SELECT
                COUNT(*)::bigint AS total_rows,
                COUNT(*) FILTER (WHERE {null_predicate})::bigint AS null_rows,
                COUNT(DISTINCT {distinct_expr})
                    FILTER (WHERE {non_null_predicate})::bigint AS distinct_keys
            FROM source
            """
        )
        row = self._execute_one(query, parameters)
        total_rows = int(row[0])
        null_rows = int(row[1])
        distinct_keys = int(row[2])
        non_null_rows = total_rows - null_rows
        duplicate_rows = max(0, non_null_rows - distinct_keys)
        uniqueness_rate = distinct_keys / non_null_rows if non_null_rows else 1.0
        null_rate = null_rows / total_rows if total_rows else 0.0
        blocking = null_rows > 0 or duplicate_rows > 0
        return ModelGrainProfile(
            model_id=model.id,
            identifier_field_ids=tuple(item.id for item in identifiers),
            total_rows=total_rows,
            null_rows=null_rows,
            distinct_non_null_keys=distinct_keys,
            duplicate_rows=duplicate_rows,
            uniqueness_rate=uniqueness_rate,
            null_rate=null_rate,
            status=QualityStatus.BLOCKING if blocking else QualityStatus.PASSED,
            message=(
                f"主标识 {identifier_label} 存在 {null_rows} 条 NULL、"
                f"{duplicate_rows} 条重复记录（共 {total_rows} 行）。"
                "换一个在数据里唯一的列做主标识，或取消它的主标识角色。"
                if blocking
                else f"主标识 {identifier_label} 在当前数据中非空且唯一。"
            ),
        )

    def _profile_relations(
        self,
        release: SemanticRelease,
        *,
        started: float,
    ) -> tuple[RelationDataProfile, ...]:
        profiles: list[RelationDataProfile] = []
        for relation in sorted(release.relations, key=lambda item: item.id):
            self._check_deadline(started)
            profiles.append(self.profile_relation(relation, release))
        return tuple(profiles)

    def profile_relation(self, relation: Any, release: SemanticRelease) -> RelationDataProfile:
        """用真实数据核对一条关系：两端命中率、实测基数与扇出倍数。"""

        model_by_id = {item.id: item for item in release.models}
        field_by_id = {item.id: item for item in release.fields}
        left_model = model_by_id[relation.left_model_id]
        right_model = model_by_id[relation.right_model_id]
        left_source, left_parameters = self._model_source(left_model, release, prefix="l_")
        right_source, right_parameters = self._model_source(right_model, release, prefix="r_")
        left_columns = [
            _quote_identifier(field_by_id[item.left_field_id].column)
            for item in relation.conditions
        ]
        right_columns = [
            _quote_identifier(field_by_id[item.right_field_id].column)
            for item in relation.conditions
        ]
        left_select = ", ".join(
            f"{column} AS k{index}" for index, column in enumerate(left_columns)
        )
        right_select = ", ".join(
            f"{column} AS k{index}" for index, column in enumerate(right_columns)
        )
        left_non_null = " AND ".join(f"{column} IS NOT NULL" for column in left_columns)
        right_non_null = " AND ".join(f"{column} IS NOT NULL" for column in right_columns)
        equality = " AND ".join(f"l.k{index} = r.k{index}" for index in range(len(left_columns)))
        query = text(
            f"""
            WITH left_source AS ({left_source}),
            right_source AS ({right_source}),
            left_keys AS (
                SELECT {left_select}, COUNT(*)::bigint AS row_count
                FROM left_source
                WHERE {left_non_null}
                GROUP BY {", ".join(f"k{index}" for index in range(len(left_columns)))}
            ),
            right_keys AS (
                SELECT {right_select}, COUNT(*)::bigint AS row_count
                FROM right_source
                WHERE {right_non_null}
                GROUP BY {", ".join(f"k{index}" for index in range(len(right_columns)))}
            ),
            left_stats AS (
                SELECT COUNT(*)::bigint AS total_rows FROM left_source
            ),
            right_stats AS (
                SELECT COUNT(*)::bigint AS total_rows FROM right_source
            ),
            joined_keys AS (
                SELECT l.row_count AS left_count, r.row_count AS right_count
                FROM left_keys l
                FULL OUTER JOIN right_keys r ON {equality}
            ),
            joined_stats AS (
                SELECT
                    COALESCE(
                        SUM(left_count) FILTER (WHERE right_count IS NOT NULL),
                        0
                    )::bigint AS matched_left_rows,
                    COALESCE(
                        SUM(right_count) FILTER (WHERE left_count IS NOT NULL),
                        0
                    )::bigint AS matched_right_rows,
                    COALESCE(MAX(left_count), 0)::bigint AS max_left_key_multiplicity,
                    COALESCE(MAX(right_count), 0)::bigint AS max_right_key_multiplicity,
                    COALESCE(
                        SUM(left_count * right_count) FILTER (
                            WHERE left_count IS NOT NULL AND right_count IS NOT NULL
                        ),
                        0
                    )::bigint AS joined_rows
                FROM joined_keys
            )
            SELECT
                left_stats.total_rows,
                right_stats.total_rows,
                joined_stats.matched_left_rows,
                joined_stats.matched_right_rows,
                joined_stats.max_left_key_multiplicity,
                joined_stats.max_right_key_multiplicity,
                joined_stats.joined_rows
            FROM left_stats
            CROSS JOIN right_stats
            CROSS JOIN joined_stats
            """
        )
        row = self._execute_one(query, {**left_parameters, **right_parameters})
        left_rows, right_rows, matched_left, matched_right, left_max, right_max, joined = (
            int(value) for value in row
        )
        observed = _observed_cardinality(left_max, right_max)
        status, message = _relation_status(
            declared=relation.cardinality,
            observed=observed,
            left_rows=left_rows,
            right_rows=right_rows,
            matched_left=matched_left,
            matched_right=matched_right,
        )
        return RelationDataProfile(
            relation_id=relation.id,
            left_rows=left_rows,
            right_rows=right_rows,
            matched_left_rows=matched_left,
            matched_right_rows=matched_right,
            orphan_left_rows=max(0, left_rows - matched_left),
            orphan_right_rows=max(0, right_rows - matched_right),
            left_join_coverage=matched_left / left_rows if left_rows else 1.0,
            right_join_coverage=matched_right / right_rows if right_rows else 1.0,
            max_left_key_multiplicity=left_max,
            max_right_key_multiplicity=right_max,
            joined_rows=joined,
            left_fanout_factor=joined / matched_left if matched_left else 0.0,
            right_fanout_factor=joined / matched_right if matched_right else 0.0,
            declared_cardinality=relation.cardinality,
            observed_cardinality=observed,
            status=status,
            message=message,
        )

    def _preview_metrics(
        self,
        release: SemanticRelease,
        *,
        started: float,
    ) -> tuple[MetricPreview, ...]:
        previews: list[MetricPreview] = []
        for dataset in sorted(release.datasets, key=lambda item: item.id):
            for metric_id in dataset.metric_ids:
                self._check_deadline(started)
                if len(previews) >= self._max_metric_previews:
                    raise ModelingQualityError(
                        "metric preview count exceeds the configured limit",
                        code="MODELING_QUALITY_SCOPE_TOO_LARGE",
                    )
                previews.append(self.preview_metric(dataset.id, metric_id, release))
        return tuple(previews)

    def preview_metric(
        self,
        dataset_id: str,
        metric_id: str,
        release: SemanticRelease,
    ) -> MetricPreview:
        """取一个指标的样本值。翻译或执行失败同样是结论，装进 preview 而不是抛出。"""

        preview_id = f"metric_preview_{uuid.uuid4().hex}"
        try:
            physical = self._translator.translate(
                release=release,
                query=SemanticQuery(dataset_id=dataset_id, metric_ids=(metric_id,)),
            )
            result = self._executor.execute(query=physical, release=release)
        except AnalyticsError as exc:
            return MetricPreview(
                id=preview_id,
                dataset_id=dataset_id,
                metric_id=metric_id,
                status=QualityStatus.BLOCKING,
                error_code=exc.code,
                message=str(exc),
            )
        return MetricPreview(
            id=preview_id,
            dataset_id=dataset_id,
            metric_id=metric_id,
            columns=result.columns,
            rows=result.rows,
            message="请与认证报表或业务负责人核对该指标样本值。",
        )

    def preview_model_rows(
        self,
        model: Any,
        release: SemanticRelease,
        *,
        limit: int = 20,
    ) -> ModelRowsPreview:
        """按受治理来源取几行真实数据。

        建模者判断一列是不是账号、一个数值是不是档位，看一眼原始行比读任何统计量都快。
        走的是模型的受治理来源（含行级过滤），不是裸表——预览里出现的行，问数时也能查到。
        """

        if not 1 <= limit <= MODEL_ROWS_PREVIEW_LIMIT:
            raise ModelingQualityError(
                f"row preview limit must be between 1 and {MODEL_ROWS_PREVIEW_LIMIT}",
                code="MODELING_QUALITY_SCOPE_TOO_LARGE",
            )
        source_sql, parameters = self._model_source(model, release)
        # 多取一行只为判断「还有更多」，不返回给调用方。
        query = text(f"WITH source AS ({source_sql}) SELECT * FROM source LIMIT {limit + 1}")
        columns, fetched = self._execute_rows(query, parameters)
        return ModelRowsPreview(
            model_id=model.id,
            columns=columns,
            rows=fetched[:limit],
            truncated=len(fetched) > limit,
        )

    @staticmethod
    def _reachability_matrix(
        release: SemanticRelease,
    ) -> tuple[DatasetReachabilityCell, ...]:
        metric_by_id = {item.id: item for item in release.metrics}
        dimension_by_id = {item.id: item for item in release.dimensions}
        planner = JoinPlanner(release.relations)
        primary_models = {
            field.model_id
            for field in release.fields
            if field.kind is FieldKind.IDENTIFIER and field.identifier_type == "primary"
        }
        cells: list[DatasetReachabilityCell] = []
        for dataset in sorted(release.datasets, key=lambda item: item.id):
            for metric_id in dataset.metric_ids:
                metric = metric_by_id[metric_id]
                for dimension_id in dataset.dimension_ids:
                    dimension = dimension_by_id[dimension_id]
                    try:
                        path = planner.plan(
                            anchor_model_id=metric.model_id,
                            required_model_ids={metric.model_id, dimension.model_id},
                            has_metrics=True,
                            allow_multiplication=True,
                        )
                        if (
                            JoinPlanner.multiplies_anchor(path)
                            and metric.model_id not in primary_models
                        ):
                            # 会被放大，而事实根没有主标识：塌不回去，也就算不对。
                            status = QualityStatus.BLOCKING
                            code = "FANOUT_RISK"
                            message = (
                                "该维度经一对多关系访问，会把事实行复制成多份；"
                                "事实模型没有主标识，无法按主标识去重还原。"
                                "确认一个在数据里唯一的主标识后即可按该维度分组。"
                            )
                        elif JoinPlanner.multiplies_anchor(path):
                            status = QualityStatus.PASSED
                            code = "REACHABLE_AFTER_COLLAPSE"
                            message = (
                                "该维度经一对多关系访问：查询会先按事实主标识去重再聚合，"
                                "同一条事实不会被重复计入。"
                            )
                        else:
                            status = QualityStatus.PASSED
                            code = "REACHABLE"
                            message = "指标可沿唯一且不扩张事实粒度的关系访问该维度。"
                    except TranslationError as exc:
                        path = ()
                        status = QualityStatus.BLOCKING
                        code = exc.code
                        message = str(exc)
                    cells.append(
                        DatasetReachabilityCell(
                            dataset_id=dataset.id,
                            metric_id=metric.id,
                            dimension_id=dimension.id,
                            metric_model_id=metric.model_id,
                            dimension_model_id=dimension.model_id,
                            relation_ids=tuple(item.relation.id for item in path),
                            status=status,
                            reason_code=code,
                            message=message,
                        )
                    )
        return tuple(cells)

    def _model_source(
        self,
        model: Any,
        release: SemanticRelease,
        *,
        prefix: str = "m_",
    ) -> tuple[str, dict[str, Any]]:
        try:
            return compile_governed_model_source(
                model,
                release,
                parameter_prefix=prefix,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelingQualityError(
                f"model {model.id} has an invalid governed source",
                code="SQL_MODEL_INVALID",
            ) from exc

    def _execute_one(self, query: Any, parameters: dict[str, Any]) -> tuple[Any, ...]:
        """质量检查的语句按内部记法拼，在这里统一落到数据源的方言。

        这些语句里有三样 MySQL 不认的东西：``::bigint`` 转型、
        ``COUNT(*) FILTER (WHERE ...)``、以及多列去重用的行构造器
        ``COUNT(DISTINCT (a, b))``（实测以 1241 拒绝）。不收口的话，MySQL 数据源上
        发布前质量报告一条都跑不出来。
        """

        return self._run(query, parameters, lambda result: tuple(result.one()))

    def _execute_rows(
        self, query: Any, parameters: dict[str, Any]
    ) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
        """同一条只读通路，取多行。数值按执行器那套规则归一，免得尾零进到界面。"""

        def fetch(result: Any) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
            columns = tuple(map(str, result.keys()))
            rows = tuple(tuple(normalize_cell(cell) for cell in row) for row in result.fetchall())
            return columns, rows

        return self._run(query, parameters, fetch)

    def _run(self, query: Any, parameters: dict[str, Any], fetch: Any) -> Any:
        statement = text(to_dialect_sql(str(query), self._dialect))
        try:
            with self._engine.connect() as connection, connection.begin():
                for item in self._dialect.read_only_session_sql(
                    statement_timeout_ms=self._statement_timeout_ms, lock_timeout_ms=2_000
                ):
                    connection.exec_driver_sql(item)
                return fetch(connection.execute(statement, parameters))
        except SQLAlchemyError as exc:
            raise ModelingQualityError(
                f"{self._dialect.value} quality profile query failed"
            ) from exc


def _quote_identifier(value: str | None) -> str:
    if not value:
        raise ModelingQualityError("quality profile requires a physical identifier")
    return '"' + value.replace('"', '""') + '"'


def _observed_cardinality(left_max: int, right_max: int) -> Cardinality | None:
    if left_max == 0 or right_max == 0:
        return None
    left_many = left_max > 1
    right_many = right_max > 1
    if left_many and right_many:
        return Cardinality.MANY_TO_MANY
    if left_many:
        return Cardinality.MANY_TO_ONE
    if right_many:
        return Cardinality.ONE_TO_MANY
    return Cardinality.ONE_TO_ONE


def _relation_status(
    *,
    declared: Cardinality,
    observed: Cardinality | None,
    left_rows: int,
    right_rows: int,
    matched_left: int,
    matched_right: int,
) -> tuple[QualityStatus, str]:
    if observed is not None and observed is not declared:
        return (
            QualityStatus.BLOCKING,
            f"声明基数为 {declared.value}，当前数据观测为 {observed.value}。",
        )
    orphan_left = max(0, left_rows - matched_left)
    orphan_right = max(0, right_rows - matched_right)
    if orphan_left or orphan_right:
        return (
            QualityStatus.WARNING,
            f"关系存在左侧 {orphan_left} 条、右侧 {orphan_right} 条未匹配记录。",
        )
    if observed is None:
        return QualityStatus.WARNING, "当前数据没有可用于证明关系基数的非空匹配键。"
    return QualityStatus.PASSED, "声明基数与当前数据一致，Join 键全部覆盖。"


def _build_report(
    *,
    revision: ModelingRevision,
    model_grains: tuple[ModelGrainProfile, ...],
    relations: tuple[RelationDataProfile, ...],
    metric_previews: tuple[MetricPreview, ...],
    reachability: tuple[DatasetReachabilityCell, ...],
) -> ModelingQualityReport:
    now = datetime.now(UTC)
    report_id = f"modeling_quality_{uuid.uuid4().hex}"
    base = {
        "project_id": revision.project_id,
        "revision_id": revision.id,
        "revision_etag": revision.etag,
        "schema_snapshot_hash": revision.schema_snapshot_hash,
        # 证据哈希只覆盖影响正确性的字段，改中文名/别名不会作废这次全表扫描。
        "semantic_spec_hash": semantic_evidence_hash(revision.semantic_spec),
        "model_grains": [item.model_dump(mode="json") for item in model_grains],
        "relations": [item.model_dump(mode="json") for item in relations],
        "metric_previews": [item.model_dump(mode="json") for item in metric_previews],
        "reachability": [item.model_dump(mode="json") for item in reachability],
    }
    blocking_count, warning_count, ready = _quality_summary(
        model_grains,
        relations,
        metric_previews,
        reachability,
    )
    return ModelingQualityReport(
        id=report_id,
        project_id=revision.project_id,
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        semantic_spec_hash=semantic_evidence_hash(revision.semantic_spec),
        etag=1,
        content_hash=content_hash(base),
        ready=ready,
        blocking_count=blocking_count,
        warning_count=warning_count,
        model_grains=model_grains,
        relations=relations,
        metric_previews=metric_previews,
        reachability=reachability,
        created_at=now,
    )


def _rebuild_report(
    report: ModelingQualityReport,
    *,
    metric_previews: tuple[MetricPreview, ...],
    status: ModelingQualityReportStatus,
    etag: int,
    reviewed_by: str,
    reviewed_at: datetime,
) -> ModelingQualityReport:
    blocking_count, warning_count, ready = _quality_summary(
        report.model_grains,
        report.relations,
        metric_previews,
        report.reachability,
    )
    base = {
        "source_content_hash": report.content_hash,
        "metric_previews": [item.model_dump(mode="json") for item in metric_previews],
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at.isoformat(),
    }
    return ModelingQualityReport(
        **report.model_dump(
            mode="python",
            exclude={
                "etag",
                "status",
                "content_hash",
                "ready",
                "blocking_count",
                "warning_count",
                "metric_previews",
                "reviewed_by",
                "reviewed_at",
            },
        ),
        etag=etag,
        status=status,
        content_hash=content_hash(base),
        ready=ready,
        blocking_count=blocking_count,
        warning_count=warning_count,
        metric_previews=metric_previews,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
    )


def _quality_summary(
    model_grains: tuple[ModelGrainProfile, ...],
    relations: tuple[RelationDataProfile, ...],
    metric_previews: tuple[MetricPreview, ...],
    reachability: tuple[DatasetReachabilityCell, ...],
) -> tuple[int, int, bool]:
    statuses = [
        # 未配置主标识的模型是「这一项查不了」,不是数据质量发现:结构诊断
        # (MODEL_PRIMARY_IDENTIFIER_MISSING)已单独报过并给了修复建议。计进
        # warning_count 会让顶部数字比列表里能看到的条目多,用户找不到差的那条。
        *(item.status for item in model_grains if item.identifier_field_ids),
        *(item.status for item in relations),
        *(item.status for item in metric_previews),
        *(item.status for item in reachability),
    ]
    blocking = sum(item in {QualityStatus.BLOCKING, QualityStatus.REJECTED} for item in statuses)
    warnings = sum(item is QualityStatus.WARNING for item in statuses)
    pending = any(item is QualityStatus.PENDING_REVIEW for item in statuses)
    return blocking, warnings, blocking == 0 and not pending


def finite_ratio(value: float) -> float:
    """Normalize driver-specific numeric results before JSON serialization."""

    return value if math.isfinite(value) else 0.0


def modeling_quality_report_is_stale(
    report: ModelingQualityReport,
    revision: Any,
    *,
    expected_etag: int,
    expected_content_hash: str,
) -> bool:
    """人工核对前判断这份质量报告是否还配得上当前 Revision。

    刻意不比较 ``semantic_spec_hash``：该字段存的是
    :func:`semantic_evidence_hash`（见本模块创建报告处），而 Revision 上的是
    ``semantic_spec.spec_hash`` —— 两个不同口径的哈希，比对必然失败。曾因此
    出现「刚跑完体检，点提交核对立刻报 stale」，而发布门禁又要求先完成这次
    核对，用户被彻底卡死。

    ``revision_etag`` 已经足够表达「草稿被编辑过」：任何改动都会推进 etag。
    """

    return (
        report.revision_id != revision.id
        or report.project_id != revision.project_id
        or report.status is not ModelingQualityReportStatus.COMPLETED
        or report.etag != expected_etag
        or report.content_hash != expected_content_hash
        or report.revision_etag != revision.etag
        or report.schema_snapshot_hash != revision.schema_snapshot_hash
    )
