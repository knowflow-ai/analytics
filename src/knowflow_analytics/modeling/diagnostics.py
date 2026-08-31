from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import Field

from knowflow_analytics.contracts import Cardinality, FieldKind, FrozenModel
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.modeling.ai_artifacts import ensure_entity_name_dimensions
from knowflow_analytics.modeling.catalog_contracts import (
    IdentifierType,
    ModelDimensionType,
)
from knowflow_analytics.modeling.contracts import ModelingRevision, RevisionState
from knowflow_analytics.modeling.revision import RevisionEditor

_DIAGNOSTIC_RESOURCE_ID_LIMIT = 1_000
_SHARED_SPELLING_PREVIEW_LIMIT = 10


class ModelingDiagnostic(FrozenModel):
    diagnostic_code: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=4_000)
    resource_kind: Literal[
        "revision",
        "model",
        "relation",
        "dimension",
        "metric",
        "field",
        "dataset",
        "golden_suite",
    ]
    affected_resource_ids: tuple[str, ...] = ()
    decision_kind: str = Field(min_length=1, max_length=128)
    blocking: bool
    recommended_action: str = Field(default="", max_length=1_000)


class ModelingDiagnosticsReport(FrozenModel):
    project_id: str
    revision_id: str
    revision_etag: int
    schema_snapshot_hash: str
    semantic_spec_hash: str
    ready: bool
    blocking_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    diagnostics: tuple[ModelingDiagnostic, ...] = ()


class ModelingDiagnosticsAnalyzer:
    """Explain publishability without inventing a second modeling contract."""

    def __init__(self, revision_editor: RevisionEditor | None = None) -> None:
        self._revision_editor = revision_editor or RevisionEditor()

    def analyze(self, revision: ModelingRevision) -> ModelingDiagnosticsReport:
        diagnostics: list[ModelingDiagnostic] = []
        catalog = revision.semantic_catalog
        release = revision.semantic_spec

        if catalog is not None:
            for model in catalog.models:
                primary_identifiers = [
                    item
                    for item in model.model_detail.identifiers
                    if item.type is IdentifierType.PRIMARY
                ]
                if not primary_identifiers:
                    diagnostics.append(
                        ModelingDiagnostic(
                            diagnostic_code="MODEL_PRIMARY_IDENTIFIER_MISSING",
                            title="模型缺少主标识",
                            message=f"模型“{model.name}”尚未确认能唯一解释一行数据的主标识。",
                            resource_kind="model",
                            affected_resource_ids=(model.id,),
                            decision_kind="confirm_identifier",
                            blocking=False,
                            recommended_action="在模型字段中将真实唯一键配置为“标识符 / 主标识”。",
                        )
                    )

                primary_times = [
                    item
                    for item in model.model_detail.dimensions
                    if item.type in {ModelDimensionType.TIME, ModelDimensionType.PARTITION_TIME}
                    and item.type_params is not None
                    and item.type_params.is_primary.casefold() == "true"
                ]
                if len(primary_times) > 1:
                    diagnostics.append(
                        ModelingDiagnostic(
                            diagnostic_code="MODEL_MULTIPLE_PRIMARY_TIME_DIMENSIONS",
                            title="模型存在多个主时间",
                            message=f"模型“{model.name}”配置了多个主时间字段，默认时间范围会产生歧义。",
                            resource_kind="model",
                            affected_resource_ids=(model.id,),
                            decision_kind="confirm_primary_time",
                            blocking=True,
                            recommended_action="只保留一个主时间，其余时间维度取消主时间标记。",
                        )
                    )

            for relation in catalog.model_relations:
                if relation.knowflow_cardinality is None:
                    diagnostics.append(
                        ModelingDiagnostic(
                            diagnostic_code="RELATION_CARDINALITY_REQUIRED",
                            title="关系基数未确认",
                            message="关系缺少一对一、一对多或多对一基数，无法判断聚合扇出风险。",
                            resource_kind="relation",
                            affected_resource_ids=(relation.id,),
                            decision_kind="confirm_relation_cardinality",
                            blocking=True,
                            recommended_action="编辑关系并按真实数据粒度确认基数。",
                        )
                    )

        for relation in release.relations:
            if relation.cardinality is Cardinality.MANY_TO_MANY:
                diagnostics.append(
                    ModelingDiagnostic(
                        diagnostic_code="RELATION_MANY_TO_MANY_REQUIRES_BRIDGE",
                        title="多对多关系不能直接用于聚合",
                        message="当前关系可能重复放大指标，必须引入桥接模型或经过验证的预聚合口径。",
                        resource_kind="relation",
                        affected_resource_ids=(relation.id,),
                        decision_kind="resolve_fanout",
                        blocking=True,
                        recommended_action="将多对多拆成两条可解释关系，或限制数据集不跨该关系查询。",
                    )
                )
            elif relation.cardinality is Cardinality.ONE_TO_MANY:
                diagnostics.append(
                    ModelingDiagnostic(
                        diagnostic_code="RELATION_FANOUT_REVIEW_REQUIRED",
                        title="一对多关系需要覆盖聚合验证",
                        message="从一侧指标下钻到多侧维度时可能产生扇出，发布前黄金问题必须覆盖该路径。",
                        resource_kind="relation",
                        affected_resource_ids=(relation.id,),
                        decision_kind="review_fanout",
                        blocking=False,
                        recommended_action="增加跨该关系的指标+维度黄金问题，并核对结果。",
                    )
                )

        metric_by_id = {item.id: item for item in release.metrics}
        dimension_by_id = {item.id: item for item in release.dimensions}
        for dataset in release.datasets:
            if not dataset.metric_ids and not dataset.dimension_ids:
                diagnostics.append(
                    ModelingDiagnostic(
                        diagnostic_code="DATASET_QUERY_SCOPE_EMPTY",
                        title="数据集没有可问资源",
                        message=f"数据集“{dataset.name}”没有开放任何指标或维度。",
                        resource_kind="dataset",
                        affected_resource_ids=(dataset.id,),
                        decision_kind="configure_dataset_scope",
                        blocking=True,
                        recommended_action="在数据集中至少开放一个经过治理的指标或维度。",
                    )
                )
            names: dict[str, list[str]] = defaultdict(list)
            for metric_id in dataset.metric_ids:
                metric = metric_by_id[metric_id]
                for name in (metric.name, *metric.aliases):
                    if name.strip():
                        names[name.strip().casefold()].append(metric.id)
            for dimension_id in dataset.dimension_ids:
                dimension = dimension_by_id[dimension_id]
                for name in (dimension.name, *dimension.aliases):
                    if name.strip():
                        names[name.strip().casefold()].append(dimension.id)
            conflicts = sorted(
                {
                    resource_id
                    for values in names.values()
                    if len(set(values)) > 1
                    for resource_id in values
                }
            )
            if conflicts:
                diagnostics.append(
                    ModelingDiagnostic(
                        diagnostic_code="DATASET_SEMANTIC_NAME_AMBIGUOUS",
                        title="数据集存在重名语义",
                        message=f"数据集“{dataset.name}”中有指标、维度或别名相同，问数时可能需要澄清。",
                        resource_kind="dataset",
                        affected_resource_ids=(dataset.id, *conflicts),
                        decision_kind="resolve_semantic_name_conflict",
                        blocking=False,
                        recommended_action="调整业务名称或别名，或拆分数据集的可问范围。",
                    )
                )

        # 跨作用域指标同名：同一说法命中不同事实模型的多个指标时，问数会
        # fail-closed 成范围澄清。逐模型的 AI 命名看不见兄弟模型，这类碰撞
        # 只能在这里跨数据集检查后暴露给人。
        queryable_metric_ids = {
            metric_id for dataset in release.datasets for metric_id in dataset.metric_ids
        }
        metric_spellings: dict[str, set[str]] = defaultdict(set)
        for metric_id in sorted(queryable_metric_ids):
            metric = metric_by_id.get(metric_id)
            if metric is None:
                continue
            for spelling in (metric.name, *metric.aliases):
                if spelling.strip():
                    metric_spellings[spelling.strip().casefold()].add(metric_id)
        shared_spellings = {
            spelling: metric_ids
            for spelling, metric_ids in metric_spellings.items()
            if len(metric_ids) > 1
            and len({metric_by_id[metric_id].model_id for metric_id in metric_ids}) > 1
        }
        if shared_spellings:
            shared_metric_ids = sorted(
                {metric_id for metric_ids in shared_spellings.values() for metric_id in metric_ids}
            )[:_DIAGNOSTIC_RESOURCE_ID_LIMIT]
            sorted_spellings = tuple(sorted(shared_spellings))
            preview_spellings = sorted_spellings[:_SHARED_SPELLING_PREVIEW_LIMIT]
            phrases = "、".join(f"“{item}”" for item in preview_spellings)
            preview_note = (
                f"（共 {len(sorted_spellings)} 个共享说法，仅展示前 {len(preview_spellings)} 个）"
                if len(sorted_spellings) > len(preview_spellings)
                else ""
            )
            diagnostics.append(
                ModelingDiagnostic(
                    diagnostic_code="CROSS_SCOPE_METRIC_NAME_SHARED",
                    title="跨作用域指标同名",
                    message=(
                        f"说法 {phrases}{preview_note} 同时指向不同事实模型的多个指标，"
                        "包含这些词的问题都会要求先选择分析范围。"
                    ),
                    resource_kind="metric",
                    affected_resource_ids=tuple(shared_metric_ids),
                    decision_kind="resolve_semantic_name_conflict",
                    blocking=False,
                    recommended_action=(
                        "为各指标补充带业务实体的可区分名称或别名，或确认接受问数时的范围澄清。"
                    ),
                )
            )

        # 实体名称维度未决：确认主标识的实体上有多个候选名列，或目标名
        # 被占用——编译器不猜（I3 合同），但必须让人看见并裁决。没有候选名列
        # 的实体不告警：那是 schema 事实（代理键表很常见），不是建模错误。
        if catalog is not None:
            _named_catalog, entity_name_resolutions = ensure_entity_name_dimensions(catalog)
            model_names = {item.id: item.name for item in catalog.models}
            for resolution in entity_name_resolutions:
                if resolution.status not in {"multiple_candidates", "name_taken"}:
                    continue
                model_name = model_names.get(resolution.model_id, resolution.model_id)
                reason = (
                    "存在多个疑似名称列，无法确定哪一列是实体名称"
                    if resolution.status == "multiple_candidates"
                    else f"目标名“{resolution.new_name}”已被其它资源占用"
                )
                diagnostics.append(
                    ModelingDiagnostic(
                        diagnostic_code="ENTITY_NAME_DIMENSION_UNRESOLVED",
                        title="实体名称维度未确定",
                        message=(
                            f"实体“{model_name}”{reason}；"
                            "「各" + model_name + "的…」类问题可能无法稳定回答。"
                        ),
                        resource_kind="model",
                        affected_resource_ids=(resolution.model_id,),
                        decision_kind="confirm_entity_name_dimension",
                        blocking=False,
                        recommended_action=(
                            "指定该实体的名称维度并命名为“实体名+名称”，或确认该实体无需名称维度。"
                        ),
                    )
                )

        # 跨作用域维度同名：与指标不同，它不触发强制澄清，而是让「各X」类
        # 问题落到错误实体上（城市/图书馆事故里「城市」被劫持到图书馆名字列，
        # 城市分析 Scope 反被判不可达）。同一维度共享给多个 Scope 是正常的，
        # 只有不同模型的不同维度撞同一说法才告警。
        queryable_dimension_ids = {
            dimension_id for dataset in release.datasets for dimension_id in dataset.dimension_ids
        }
        dimension_spellings: dict[str, set[str]] = defaultdict(set)
        for dimension_id in sorted(queryable_dimension_ids):
            dimension = dimension_by_id.get(dimension_id)
            if dimension is None:
                continue
            for spelling in (dimension.name, *dimension.aliases):
                if spelling.strip():
                    dimension_spellings[spelling.strip().casefold()].add(dimension_id)
        shared_dimension_spellings = {
            spelling: dimension_ids
            for spelling, dimension_ids in dimension_spellings.items()
            if len(dimension_ids) > 1
            and len({dimension_by_id[item].model_id for item in dimension_ids}) > 1
        }
        if shared_dimension_spellings:
            shared_dimension_ids = sorted(
                {
                    dimension_id
                    for dimension_ids in shared_dimension_spellings.values()
                    for dimension_id in dimension_ids
                }
            )[:_DIAGNOSTIC_RESOURCE_ID_LIMIT]
            sorted_spellings = tuple(sorted(shared_dimension_spellings))
            preview = sorted_spellings[:_SHARED_SPELLING_PREVIEW_LIMIT]
            phrases = "、".join(f"“{item}”" for item in preview)
            preview_note = (
                f"（共 {len(sorted_spellings)} 个共享说法，仅展示前 {len(preview)} 个）"
                if len(sorted_spellings) > len(preview)
                else ""
            )
            diagnostics.append(
                ModelingDiagnostic(
                    diagnostic_code="CROSS_SCOPE_DIMENSION_NAME_SHARED",
                    title="跨作用域维度同名",
                    message=(
                        f"说法 {phrases}{preview_note} 同时指向不同模型的多个维度，"
                        "按该词分组或过滤的问题可能落到错误的业务实体上。"
                    ),
                    resource_kind="dimension",
                    affected_resource_ids=tuple(shared_dimension_ids),
                    decision_kind="resolve_semantic_name_conflict",
                    blocking=False,
                    recommended_action=(
                        "确认每个维度的业务名指向正确实体；描述本表实体自身的列不应沿用其它实体的名字。"
                    ),
                )
            )

        # 疑似借名：非标识列的业务名携带其它实体名，而物理列名与本模型名都
        # 与该实体无关——这是 AI 命名把别的表的名字写到了本表实体属性上，
        # 负信息比零信息危险（借来的名字会劫持词义且看起来正常）。物理列名
        # 本身含实体名的（合法反规范化列）不告警，schema 事实优先。
        model_by_id = {item.id: item for item in release.models}
        for field_item in release.fields:
            if field_item.kind is FieldKind.IDENTIFIER:
                continue
            if field_item.name.strip().casefold() == field_item.column.strip().casefold():
                continue
            own_model = model_by_id.get(field_item.model_id)
            own_name = own_model.name if own_model is not None else ""
            for other in release.models:
                if other.id == field_item.model_id or len(other.name.strip()) < 2:
                    continue
                entity = other.name.strip()
                if entity not in field_item.name:
                    continue
                if entity in own_name or entity in field_item.column:
                    continue
                diagnostics.append(
                    ModelingDiagnostic(
                        diagnostic_code="FIELD_NAME_BORROWS_ENTITY_NAME",
                        title="字段疑似借用其它实体名",
                        message=(
                            f"模型“{own_name}”的列 {field_item.column} 被命名为"
                            f"“{field_item.name}”，携带了实体“{entity}”的名字，"
                            "但物理列名与本模型都与该实体无关。请确认这一列描述的是谁。"
                        ),
                        resource_kind="field",
                        affected_resource_ids=(field_item.id,),
                        decision_kind="resolve_semantic_name_conflict",
                        blocking=False,
                        recommended_action=(
                            "若该列描述本表实体自身，请改回以本表实体命名；"
                            "若确为引用其它实体的反规范化列，确认后可保留。"
                        ),
                    )
                )
                break

        # 整张表进不了任何 Scope：它的字段全部不可问。既无主标识也无业务度量的
        # 纯事件/桥接表会落到这里（音乐六表的 台湾金曲奖），用户问它时得到的是
        # "请选择分析范围"，而选哪个都答不了。
        scoped_model_ids = {
            model_id for dataset in release.datasets for model_id in dataset.model_ids
        }
        orphan_models = [item for item in release.models if item.id not in scoped_model_ids]
        if orphan_models:
            diagnostics.append(
                ModelingDiagnostic(
                    diagnostic_code="MODEL_OUTSIDE_EVERY_QUERY_SCOPE",
                    title="模型不在任何查询作用域中",
                    message=(
                        "模型 "
                        + "、".join(f"“{item.name}”" for item in orphan_models[:5])
                        + " 没有进入任何查询作用域，其字段无法被问到。"
                        "既无主标识也无业务度量的表不会成为事实根。"
                    ),
                    resource_kind="model",
                    affected_resource_ids=tuple(item.id for item in orphan_models)[
                        :_DIAGNOSTIC_RESOURCE_ID_LIMIT
                    ],
                    decision_kind="configure_dataset_scope",
                    blocking=False,
                    recommended_action=(
                        "确认该表的主标识，或把它的可聚合列配置为业务度量；"
                        "若该表确实不需要被问到，可忽略。"
                    ),
                )
            )

        # 事实根连得到、但冻结路由到不了的实体：按它的维度分组必然失败。成因是
        # 扇出（一对多）或路径不唯一（同一实体被多条外键引用，音乐六表里
        # 翻唱歌曲 经三条路径可达 歌手），两者都让该实体整体退出该作用域。
        relations_by_model: dict[str, set[str]] = defaultdict(set)
        for relation in release.relations:
            relations_by_model[relation.left_model_id].add(relation.right_model_id)
            relations_by_model[relation.right_model_id].add(relation.left_model_id)
        model_names = {item.id: item.name for item in release.models}
        datasets_by_id = {item.id: item for item in release.datasets}
        for route in release.analysis_topic_routes:
            dataset = datasets_by_id.get(route.dataset_id)
            if dataset is None:
                continue
            reachable = {route.root_model_id, *(path.target_model_id for path in route.paths)}
            connected: set[str] = set()
            frontier = [route.root_model_id]
            while frontier:
                current = frontier.pop()
                for neighbour in sorted(relations_by_model.get(current, ())):
                    if neighbour not in connected:
                        connected.add(neighbour)
                        frontier.append(neighbour)
            missing = sorted(connected - reachable)
            if not missing:
                continue
            diagnostics.append(
                ModelingDiagnostic(
                    diagnostic_code="SCOPE_ENTITY_NOT_REACHABLE",
                    title="作用域无法按这些实体分组",
                    message=(
                        f"作用域“{dataset.name}”与 "
                        + "、".join(f"“{model_names.get(item, item)}”" for item in missing[:5])
                        + " 有关联，但冻结路由到不了它们（扇出或路径不唯一），"
                        "按这些实体的维度分组或过滤的问题会被拒绝。"
                    ),
                    resource_kind="dataset",
                    affected_resource_ids=(dataset.id, *missing)[:_DIAGNOSTIC_RESOURCE_ID_LIMIT],
                    decision_kind="resolve_fanout",
                    blocking=False,
                    recommended_action=(
                        "确认关系基数，或为多路径引用的实体分别建立具名角色维度；"
                        "若该作用域本就不需要这些实体，可忽略。"
                    ),
                )
            )

        # 只读版本跳过这段发布校验预演：_require_editable 对 FROZEN/PUBLISHED
        # 一律抛 RevisionConflictError，会让每个已发布版本都稳定出现一条
        # "published revisions are immutable" 的阻断项。那是版本状态而非建模
        # 问题，用户对它无能为力。草稿仍需要这段预告。
        if revision.state not in {RevisionState.FROZEN, RevisionState.PUBLISHED}:
            try:
                self._revision_editor.validate_for_publish(revision)
            except AnalyticsError as exc:
                if not any(item.diagnostic_code == exc.code for item in diagnostics):
                    diagnostics.append(self._publish_validation_diagnostic(revision, exc))

        blocking_count = sum(item.blocking for item in diagnostics)
        warning_count = len(diagnostics) - blocking_count
        return ModelingDiagnosticsReport(
            project_id=revision.project_id,
            revision_id=revision.id,
            revision_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            semantic_spec_hash=revision.semantic_spec.spec_hash,
            ready=blocking_count == 0,
            blocking_count=blocking_count,
            warning_count=warning_count,
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _publish_validation_diagnostic(
        revision: ModelingRevision,
        exc: AnalyticsError,
    ) -> ModelingDiagnostic:
        if exc.code == "DATASET_REQUIRED":
            return ModelingDiagnostic(
                diagnostic_code=exc.code,
                title="尚未创建问数数据集",
                message="模型已导入，但还没有定义允许问数使用的维度、指标和模型范围。",
                resource_kind="dataset",
                affected_resource_ids=(),
                decision_kind="configure_dataset_scope",
                blocking=True,
                recommended_action="先确认维度和指标，再到“数据集管理”创建数据集。",
            )
        return ModelingDiagnostic(
            diagnostic_code=exc.code,
            title="结构校验未通过",
            message=str(exc),
            resource_kind="revision",
            affected_resource_ids=(revision.id,),
            decision_kind="fix_validation_error",
            blocking=True,
            recommended_action="根据错误信息修正对应语义资源后重新校验。",
        )
