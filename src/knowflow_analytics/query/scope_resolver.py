"""Pure, evidence-driven QueryScope resolution.

Frozen stage contract (``CANDIDATE_DISCOVERY``): the resolver consumes only
scope-neutral mapper evidence plus the published QueryScope projection.  It may
select a scope, ask for clarification, or refuse an unsupported/stale shape.  It
does not inspect question wording, compare match scores, choose semantic members,
or produce textual S2SQL.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol

from knowflow_analytics.contracts import (
    AnalysisTopicRouteSpec,
    Cardinality,
    DatasetSpec,
    DimensionSpec,
    DimensionValueSpec,
    FrozenModel,
    MetricSpec,
    RelationSpec,
    SemanticRelease,
)


class QueryScopeEvidence(Protocol):
    """Minimum scope-neutral evidence accepted by :class:`QueryScopeResolver`.

    ``SchemaMatch`` already satisfies this protocol.  Its legacy ``dataset_id``
    and ``score`` fields are deliberately absent: neither is authoritative when
    resolving a fact scope.
    """

    element_type: object
    element_id: str
    detected_text: str
    method: object


class _Identified(Protocol):
    id: str


class QueryScopeResolutionStatus(StrEnum):
    SELECTED = "selected"
    CLARIFICATION = "clarification"
    REFUSED = "refused"


class MetricPhraseAmbiguity(FrozenModel):
    """Several governed metrics recalled by one normalized detected phrase."""

    detected_text: str
    metric_ids: tuple[str, ...]


class QueryScopeResolution(FrozenModel):
    """A diagnostic-only Scope decision; never a parsed query or S2SQL draft."""

    status: QueryScopeResolutionStatus
    code: str
    message: str
    selected_dataset_id: str | None = None
    candidate_dataset_ids: tuple[str, ...] = ()
    owner_model_ids: tuple[str, ...] = ()
    exact_metric_ids: tuple[str, ...] = ()
    # A user-confirmed Mapper candidate fixes routing ownership without
    # falsifying its original keyword/embedding/Term provenance as EXACT.
    confirmed_metric_ids: tuple[str, ...] = ()
    # A bounded AI adjudication is an independent routing signal. It is never
    # reported as exact evidence or as a human confirmation.
    ai_adjudicated_metric_ids: tuple[str, ...] = ()
    # Explicit confirmation memory is neither a current human click nor an AI
    # decision. It remains release/candidate-bound provenance of its own.
    memory_confirmed_metric_ids: tuple[str, ...] = ()
    exact_dimension_ids: tuple[str, ...] = ()
    ambiguous_metric_groups: tuple[MetricPhraseAmbiguity, ...] = ()
    # Exact dataset-name evidence that actually narrowed the scope decision.
    # Diagnostic and narrowing-only: an anchor can never add a scope that the
    # semantic evidence filters rejected.
    anchor_dataset_ids: tuple[str, ...] = ()

    def to_trace_detail(self) -> dict[str, object]:
        """Return the JSON-safe shape expected by query trace details."""

        return self.model_dump(mode="json")


class QueryScopeResolver:
    """Resolve one governed QueryScope without inferring query meaning.

    Resolution order is part of the contract:

    1. filter evidence by allowed/selected published Dataset membership;
    2. group exact evidence by normalized ``detected_text``;
    3. clarify a phrase that maps to several metrics;
    4. reject independent metrics owned by different fact models;
    5. filter the owner's scopes by exact metric membership and exact
       dimension/value reachability through the frozen route;
    6. select a sole feasible scope or return a stable clarification/refusal.
    """

    def __init__(
        self,
        *,
        datasets: Iterable[DatasetSpec],
        routes: Iterable[AnalysisTopicRouteSpec],
        metrics: Iterable[MetricSpec],
        dimensions: Iterable[DimensionSpec],
        values: Iterable[DimensionValueSpec],
        relations: Iterable[RelationSpec] = (),
    ) -> None:
        self._datasets = _index_by_id(datasets, label="dataset")
        self._routes = _index_by_dataset_id(routes)
        self._metrics = _index_by_id(metrics, label="metric")
        self._dimensions = _index_by_id(dimensions, label="dimension")
        self._values = _index_by_id(values, label="dimension value")
        self._relations = {item.id: item for item in relations}

    @classmethod
    def from_release(cls, release: SemanticRelease) -> QueryScopeResolver:
        return cls(
            datasets=release.datasets,
            routes=release.analysis_topic_routes,
            metrics=release.metrics,
            dimensions=release.dimensions,
            values=release.dimension_values,
            relations=release.relations,
        )

    def resolve(
        self,
        evidence: Iterable[QueryScopeEvidence],
        *,
        allowed_dataset_ids: Iterable[str] | None = None,
        selected_dataset_id: str | None = None,
        selected_element_id: str | None = None,
        selected_element_type: str | None = None,
        human_confirmed_metric_ids: Iterable[str] = (),
        ai_adjudicated_metric_id: str | None = None,
        ai_adjudicated_metric_ids: Iterable[str] = (),
        memory_confirmed_metric_id: str | None = None,
        memory_confirmed_metric_ids: Iterable[str] = (),
    ) -> QueryScopeResolution:
        evidence = tuple(evidence)
        allowed = self._allowed_dataset_ids(allowed_dataset_ids)
        if selected_dataset_id is not None and selected_dataset_id not in allowed:
            return self._resolution(
                status=QueryScopeResolutionStatus.REFUSED,
                code="SELECTED_QUERY_SCOPE_INVALID",
                message="确认的业务分析对象不属于当前允许范围。",
            )
        # Exact dataset-name evidence is a deterministic scope anchor. The
        # global longest-word scan means a verbatim scope name consumes its
        # span and starves shorter metric evidence, so without this the more
        # precisely a user names a scope the wider the clarification gets.
        # Anchors only ever narrow candidate sets and are dropped whenever the
        # semantic evidence filters disagree; an explicit confirmed selection
        # wins over any implicit mention. Retired dataset mentions are ignored
        # rather than refused: real index drift is caught by the semantic
        # staleness gate below.
        anchor_dataset_ids = (
            frozenset(
                item.element_id
                for item in evidence
                if _token(item.element_type) == "dataset"
                and _is_exact_direct_evidence(item)
                and item.element_id in self._datasets
                and item.element_id in allowed
                # 只有完整规范名是专指：编译器自动注册的裸模型名别名
                # （「图书馆」）是通用名词，零语义证据时会钉死错误作用域。
                and _normalize_detected_text(item.detected_text)
                == _normalize_detected_text(self._datasets[item.element_id].name)
            )
            if selected_dataset_id is None
            else frozenset()
        )
        semantic_evidence = tuple(
            item
            for item in evidence
            if _token(item.element_type) in {"metric", "dimension", "dimension_value"}
        )
        exact_semantic_evidence = tuple(
            item for item in semantic_evidence if _is_exact_direct_evidence(item)
        )
        if any(
            not self._is_current_evidence(item) or not self._evidence_scope_ids(item)
            for item in exact_semantic_evidence
        ):
            return self._resolution(
                status=QueryScopeResolutionStatus.REFUSED,
                code="QUERY_SCOPE_COMPILATION_STALE",
                message="精确语义证据与当前发布版本的分析能力不一致。",
            )
        # This visibility gate must precede every detected-text group. Otherwise
        # an out-of-scope element changes the result or appears in diagnostics,
        # turning ambiguity handling into a semantic-resource existence oracle.
        allowed_evidence = tuple(
            item
            for item in exact_semantic_evidence
            if self._evidence_scope_ids(item).intersection(allowed)
        )
        confirmed_metric_ids: set[str] = set()
        ai_metric_ids: set[str] = set()
        memory_confirmed_metric_ids: set[str] = set()
        requested_ai_metric_ids = tuple(
            dict.fromkeys(
                (
                    *(str(item) for item in ai_adjudicated_metric_ids),
                    *((ai_adjudicated_metric_id,) if ai_adjudicated_metric_id else ()),
                )
            )
        )
        requested_human_metric_ids = tuple(
            dict.fromkeys(str(item) for item in human_confirmed_metric_ids)
        )
        requested_memory_metric_ids = tuple(
            dict.fromkeys(
                (
                    *(str(item) for item in memory_confirmed_metric_ids),
                    *((memory_confirmed_metric_id,) if memory_confirmed_metric_id else ()),
                )
            )
        )
        selection_sources = sum(
            bool(item)
            for item in (
                selected_element_id,
                requested_human_metric_ids,
                requested_ai_metric_ids,
                requested_memory_metric_ids,
            )
        )
        if selection_sources > 1:
            return self._resolution(
                status=QueryScopeResolutionStatus.REFUSED,
                code="MULTIPLE_METRIC_SELECTION_SOURCES",
                message="同一次查询不能同时使用人工、记忆与 AI 指标选择。",
            )
        if selected_element_type is not None and selected_element_id is None:
            return self._resolution(
                status=QueryScopeResolutionStatus.REFUSED,
                code="SELECTED_ELEMENT_INVALID",
                message="语义元素类型必须与元素标识同时确认。",
            )
        selections = (
            (("human", selected_element_id, selected_element_type),)
            if selected_element_id is not None
            else tuple(("human", item, "metric") for item in requested_human_metric_ids)
            if requested_human_metric_ids
            else tuple(("ai", item, "metric") for item in requested_ai_metric_ids)
            if requested_ai_metric_ids
            else tuple(("memory", item, "metric") for item in requested_memory_metric_ids)
            if requested_memory_metric_ids
            else ()
        )
        for selection_source, current_element_id, current_element_type in selections:
            assert current_element_id is not None
            selectable_evidence = tuple(
                item
                for item in semantic_evidence
                if _is_selectable_evidence(item)
                and self._is_current_evidence(item)
                and self._evidence_scope_ids(item)
            )
            matching_evidence = tuple(
                item for item in selectable_evidence if item.element_id == current_element_id
            )
            if not matching_evidence:
                return self._resolution(
                    status=QueryScopeResolutionStatus.REFUSED,
                    code="SELECTED_ELEMENT_INVALID",
                    message="确认的语义元素不属于当前精确证据。",
                )
            allowed_matching_evidence = tuple(
                item
                for item in matching_evidence
                if self._evidence_scope_ids(item).intersection(allowed)
            )
            if not allowed_matching_evidence:
                return self._resolution(
                    status=QueryScopeResolutionStatus.REFUSED,
                    code="SELECTED_ELEMENT_SCOPE_VIOLATION",
                    message="确认的语义元素不属于当前允许的业务分析范围。",
                )
            matching_types = {_token(item.element_type) for item in allowed_matching_evidence}
            if current_element_type is None:
                if len(matching_types) > 1:
                    return self._resolution(
                        status=QueryScopeResolutionStatus.REFUSED,
                        code="SELECTED_ELEMENT_TYPE_REQUIRED",
                        message="同一元素标识对应多种语义资源，请同时确认元素类型。",
                    )
                effective_selected_element_type = next(iter(matching_types))
            else:
                effective_selected_element_type = _token(current_element_type)
                if effective_selected_element_type not in matching_types:
                    all_matching_types = {_token(item.element_type) for item in matching_evidence}
                    code = (
                        "SELECTED_ELEMENT_SCOPE_VIOLATION"
                        if effective_selected_element_type in all_matching_types
                        else "SELECTED_ELEMENT_INVALID"
                    )
                    return self._resolution(
                        status=QueryScopeResolutionStatus.REFUSED,
                        code=code,
                        message="确认的语义元素类型不属于当前 Mapper 证据。",
                    )
            selected_matching_evidence = tuple(
                item
                for item in allowed_matching_evidence
                if _token(item.element_type) == effective_selected_element_type
            )
            if selection_source == "ai" and any(
                _is_exact_direct_evidence(item) for item in selected_matching_evidence
            ):
                return self._resolution(
                    status=QueryScopeResolutionStatus.REFUSED,
                    code="AI_ADJUDICATION_REQUIRES_WEAK_METRIC",
                    message="AI 指标裁决只能消费非精确 Mapper 候选。",
                )
            if effective_selected_element_type == "metric" and not any(
                _is_exact_direct_evidence(item) for item in selected_matching_evidence
            ):
                if selection_source == "ai":
                    ai_metric_ids.add(current_element_id)
                elif selection_source == "memory":
                    memory_confirmed_metric_ids.add(current_element_id)
                else:
                    confirmed_metric_ids.add(current_element_id)
            allowed_evidence = self._apply_selected_element(
                allowed_evidence,
                selected_element_id=current_element_id,
                selected_element_type=effective_selected_element_type,
            )
        if selected_dataset_id is not None:
            if not self._evidence_groups_fit_dataset(
                allowed_evidence,
                dataset_id=selected_dataset_id,
            ):
                return self._resolution(
                    status=QueryScopeResolutionStatus.REFUSED,
                    code="SELECTED_QUERY_SCOPE_INVALID",
                    message="确认的业务分析对象无法满足全部精确语义条件。",
                )
            allowed_evidence = tuple(
                item
                for item in allowed_evidence
                if selected_dataset_id in self._evidence_scope_ids(item)
            )
        exact = allowed_evidence
        compatible_allowed = {
            dataset_id
            for dataset_id in allowed
            if self._evidence_groups_fit_dataset(exact, dataset_id=dataset_id)
        }
        metric_phrases: dict[str, set[str]] = defaultdict(set)
        dimension_phrases: dict[str, set[str]] = defaultdict(set)
        value_phrases: dict[str, set[str]] = defaultdict(set)
        direct_dimension_ids: set[str] = set()
        exact_value_ids: set[str] = set()
        for item in exact:
            element_type = _token(item.element_type)
            detected_text = _normalize_detected_text(item.detected_text)
            if element_type == "metric":
                metric_phrases[detected_text].add(item.element_id)
            elif element_type == "dimension":
                dimension_phrases[detected_text].add(item.element_id)
                direct_dimension_ids.add(item.element_id)
            elif element_type == "dimension_value":
                value_phrases[detected_text].add(item.element_id)
                exact_value_ids.add(item.element_id)

        exact_metric_ids = {
            metric_id for metric_ids in metric_phrases.values() for metric_id in metric_ids
        }
        routing_metric_ids = (
            exact_metric_ids | confirmed_metric_ids | ai_metric_ids | memory_confirmed_metric_ids
        )
        missing_semantics = (
            exact_metric_ids - self._metrics.keys()
            | direct_dimension_ids - self._dimensions.keys()
            | exact_value_ids - self._values.keys()
        )
        disabled_values = {
            value_id
            for value_id in exact_value_ids
            if value_id in self._values and not self._values[value_id].enabled
        }
        if missing_semantics or disabled_values:
            return self._resolution(
                status=QueryScopeResolutionStatus.REFUSED,
                code="QUERY_SCOPE_COMPILATION_STALE",
                message="精确语义证据与当前发布版本的分析能力不一致。",
                exact_metric_ids=exact_metric_ids,
                exact_dimension_ids=direct_dimension_ids,
            )

        for detected_text, value_ids in value_phrases.items():
            dimension_phrases[detected_text].update(
                self._values[value_id].dimension_id for value_id in value_ids
            )
        dimension_groups = tuple(
            frozenset(dimension_ids)
            for _detected_text, dimension_ids in sorted(dimension_phrases.items())
        )
        exact_dimension_ids = {
            dimension_id for dimension_ids in dimension_groups for dimension_id in dimension_ids
        }
        missing_value_dimensions = exact_dimension_ids - self._dimensions.keys()
        if missing_value_dimensions:
            return self._resolution(
                status=QueryScopeResolutionStatus.REFUSED,
                code="QUERY_SCOPE_COMPILATION_STALE",
                message="精确语义证据与当前发布版本的分析能力不一致。",
                exact_metric_ids=exact_metric_ids,
                exact_dimension_ids=exact_dimension_ids,
            )

        metric_groups = tuple(
            frozenset(metric_ids) for _detected_text, metric_ids in sorted(metric_phrases.items())
        )
        ambiguities = tuple(
            MetricPhraseAmbiguity(
                detected_text=detected_text,
                metric_ids=tuple(sorted(metric_ids)),
            )
            for detected_text, metric_ids in sorted(metric_phrases.items())
            if len(metric_ids) > 1
        )
        if ambiguities:
            candidates = self._ambiguous_metric_scope_ids(
                allowed=compatible_allowed,
                metric_groups=metric_groups,
                dimension_groups=dimension_groups,
            )
            if not candidates:
                return self._resolution(
                    status=QueryScopeResolutionStatus.REFUSED,
                    code="NO_COMPATIBLE_QUERY_SCOPE",
                    message="没有业务分析路径能同时满足全部精确语义条件。",
                    exact_metric_ids=exact_metric_ids,
                    exact_dimension_ids=exact_dimension_ids,
                    ambiguities=ambiguities,
                )
            ambiguity_owners = {
                self._metrics[metric_id].model_id
                for group in ambiguities
                for metric_id in group.metric_ids
            }
            cross_root = len(ambiguity_owners) > 1
            # Scope routing owns only the fact boundary. Once a sole Scope (or
            # an explicit Scope continuation) is known, same-root semantic
            # alternatives stay in the Mapper result for the frozen final-LLM
            # + settlement contract. Pre-LLM element clarification remains the
            # service's same-display-name gate.
            # A dataset anchor that intersects the ambiguity candidates fixes
            # the fact boundary, so the cross-root phrase collapses inside the
            # anchored scope's membership just like a same-root alternative.
            # A single feasible scope is the same situation without the anchor:
            # a one-option clarification is not a decision, and the phrase
            # resolves inside that scope's membership.
            if (
                not cross_root
                or anchor_dataset_ids.intersection(candidates)
                or len(candidates) == 1
            ):
                return self._finish_scope_cardinality(
                    candidates=candidates,
                    selected_dataset_id=selected_dataset_id,
                    owners=ambiguity_owners,
                    exact_metric_ids=exact_metric_ids,
                    confirmed_metric_ids=confirmed_metric_ids,
                    ai_adjudicated_metric_ids=ai_metric_ids,
                    memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                    exact_dimension_ids=exact_dimension_ids,
                    anchors=anchor_dataset_ids,
                )
            return self._resolution(
                status=QueryScopeResolutionStatus.CLARIFICATION,
                code=("AMBIGUOUS_QUERY_SCOPE" if cross_root else "AMBIGUOUS_METRIC_PHRASE"),
                message=(
                    "同一精确短语可落在多个事实范围，请先确认分析对象。"
                    if cross_root
                    else "同一精确短语匹配到多个受治理指标，请先确认指标语义。"
                ),
                candidates=candidates,
                owners=ambiguity_owners,
                exact_metric_ids=exact_metric_ids,
                exact_dimension_ids=exact_dimension_ids,
                ambiguities=ambiguities,
            )

        owners = {self._metrics[metric_id].model_id for metric_id in routing_metric_ids}
        if len(owners) > 1:
            multi_fact_allowed: set[str] = set()
            for owner in owners:
                owner_evidence = tuple(
                    item
                    for item in exact
                    if _token(item.element_type) != "metric"
                    or self._metrics[item.element_id].model_id == owner
                )
                multi_fact_allowed.update(
                    dataset_id
                    for dataset_id in allowed
                    if self._evidence_groups_fit_dataset(
                        owner_evidence,
                        dataset_id=dataset_id,
                    )
                )
            candidates = self._multi_fact_scope_ids(
                allowed=multi_fact_allowed,
                metric_ids=routing_metric_ids,
                dimension_groups=dimension_groups,
            )
            return self._resolution(
                status=QueryScopeResolutionStatus.REFUSED,
                code="CROSS_FACT_METRICS_UNSUPPORTED",
                message=(
                    "多个独立精确指标属于不同事实根；请拆分问题，或先建立受治理的联合事实模型。"
                ),
                candidates=candidates,
                owners=owners,
                exact_metric_ids=exact_metric_ids,
                confirmed_metric_ids=confirmed_metric_ids,
                ai_adjudicated_metric_ids=ai_metric_ids,
                memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                exact_dimension_ids=exact_dimension_ids,
            )

        if owners:
            owner = next(iter(owners))
            owner_scopes = {
                dataset_id
                for dataset_id in compatible_allowed
                if (
                    (route := self._routes.get(dataset_id)) is not None
                    and route.root_model_id == owner
                )
            }
            member_scopes = {
                dataset_id
                for dataset_id in owner_scopes
                if routing_metric_ids.issubset(self._datasets[dataset_id].metric_ids)
            }
            if not member_scopes:
                # `compatible_allowed` 已按维度可见性过滤过，所以"作用域到不了
                # 这个维度"会在这里表现为空集。两者必须分开报：STALE 说的是
                # 编译漂移（该事实根根本没有含这些指标的作用域，提示重新发布
                # 才有意义），而路径到不了维度重新发布多少次都一样。
                metric_scopes = {
                    dataset_id
                    for dataset_id in allowed
                    if (route := self._routes.get(dataset_id)) is not None
                    and route.root_model_id == owner
                    and routing_metric_ids.issubset(self._datasets[dataset_id].metric_ids)
                }
                if metric_scopes:
                    return self._resolution(
                        status=QueryScopeResolutionStatus.REFUSED,
                        code="DIMENSION_NOT_REACHABLE",
                        message="该指标不能按所选维度安全分析。",
                        owners=owners,
                        exact_metric_ids=exact_metric_ids,
                        confirmed_metric_ids=confirmed_metric_ids,
                        ai_adjudicated_metric_ids=ai_metric_ids,
                        memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                        exact_dimension_ids=exact_dimension_ids,
                    )
                return self._resolution(
                    status=QueryScopeResolutionStatus.REFUSED,
                    code="QUERY_SCOPE_COMPILATION_STALE",
                    message="当前发布版本缺少该指标所需的安全分析路径。",
                    owners=owners,
                    exact_metric_ids=exact_metric_ids,
                    confirmed_metric_ids=confirmed_metric_ids,
                    ai_adjudicated_metric_ids=ai_metric_ids,
                    memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                    exact_dimension_ids=exact_dimension_ids,
                )
            candidates = self._dimension_reachable_scope_ids(
                dataset_ids=member_scopes,
                dimension_groups=dimension_groups,
            )
            if not candidates:
                return self._resolution(
                    status=QueryScopeResolutionStatus.REFUSED,
                    code="DIMENSION_NOT_REACHABLE",
                    message="该指标不能按所选维度安全分析。",
                    owners=owners,
                    exact_metric_ids=exact_metric_ids,
                    confirmed_metric_ids=confirmed_metric_ids,
                    ai_adjudicated_metric_ids=ai_metric_ids,
                    memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                    exact_dimension_ids=exact_dimension_ids,
                )
        else:
            routed_scopes = {
                dataset_id for dataset_id in compatible_allowed if dataset_id in self._routes
            }
            candidates = self._dimension_reachable_scope_ids(
                dataset_ids=routed_scopes,
                dimension_groups=dimension_groups,
            )
            if not candidates:
                code = (
                    "DIMENSION_NOT_REACHABLE" if exact_dimension_ids else "NO_QUERY_SCOPE_AVAILABLE"
                )
                message = (
                    "没有业务分析路径能满足每个精确维度条件。"
                    if exact_dimension_ids
                    else "当前允许范围内没有可执行的业务分析路径。"
                )
                return self._resolution(
                    status=QueryScopeResolutionStatus.REFUSED,
                    code=code,
                    message=message,
                    exact_dimension_ids=exact_dimension_ids,
                )

        return self._finish_scope_cardinality(
            candidates=candidates,
            selected_dataset_id=selected_dataset_id,
            owners=owners,
            exact_metric_ids=exact_metric_ids,
            confirmed_metric_ids=confirmed_metric_ids,
            ai_adjudicated_metric_ids=ai_metric_ids,
            memory_confirmed_metric_ids=memory_confirmed_metric_ids,
            exact_dimension_ids=exact_dimension_ids,
            anchors=anchor_dataset_ids,
        )

    def _has_business_metric(self, dataset_id: str) -> bool:
        """该 scope 是否拥有默认计数之外的业务指标。

        事实→实体的关系（订单→客户）与明细→主表（订单明细→订单）在
        many_to_one 形态上不可区分；区分信号是收敛目标必须是真正的事实
        分析对象——只有实体计数的 scope（「客户范围」）不作为收敛去向。
        """

        dataset = self._datasets.get(dataset_id)
        route = self._routes.get(dataset_id)
        if dataset is None:
            return False
        default_count = route.default_count_metric_id if route is not None else None
        return any(metric_id != default_count for metric_id in dataset.metric_ids)

    def _fine_to_coarse(self, fine_id: str, coarse_id: str) -> bool:
        """细 scope 的事实根是否经冻结单跳从属于粗 scope 的事实根。

        同一从属既可表达为 fine —many_to_one→ coarse，也可表达为
        coarse —one_to_many→ fine，两种建模都要认。
        """

        fine_route = self._routes.get(fine_id)
        coarse_route = self._routes.get(coarse_id)
        if fine_route is None or coarse_route is None:
            return False
        for path in fine_route.paths:
            if path.target_model_id != coarse_route.root_model_id:
                continue
            if len(path.relation_ids) != 1:
                continue
            relation = self._relations.get(path.relation_ids[0])
            if relation is None:
                continue
            forward = (
                relation.left_model_id == fine_route.root_model_id
                and relation.right_model_id == coarse_route.root_model_id
                and relation.cardinality is Cardinality.MANY_TO_ONE
            )
            backward = (
                relation.left_model_id == coarse_route.root_model_id
                and relation.right_model_id == fine_route.root_model_id
                and relation.cardinality is Cardinality.ONE_TO_MANY
            )
            if forward or backward:
                return True
        return False

    def _granularity_convergence(
        self,
        candidates: set[str],
        evidence_element_ids: set[str],
    ) -> str | None:
        """粒度全序链上的确定性收敛（2026-08-31 评审）。

        候选 scope 的事实根两两构成冻结 many_to_one 从属链（订单明细→订单）时，
        选择能覆盖全部 exact 语义证据的最粗 scope：细粒度独有证据（「商品」）
        自然落到细，谁都覆盖不了自然维持 fail-closed，零证据落最粗——粗粒度
        无扇出，是没有提及明细实体时最保守的解释。不读问题文本、不比较任何
        分数；非从属的多根（独立业务实体）不适用，保持原有澄清。
        """

        if len(candidates) < 2:
            return None
        ordered = sorted(candidates)
        # 全序检查：任两个候选之间必须存在唯一方向的从属关系。
        finer_than: dict[str, set[str]] = {item: set() for item in ordered}
        for a in ordered:
            for b in ordered:
                if a == b:
                    continue
                a_fine = self._fine_to_coarse(a, b)
                b_fine = self._fine_to_coarse(b, a)
                if a_fine == b_fine:
                    return None
                if a_fine:
                    finer_than[a].add(b)
        # 按「比多少个候选更细」排序：0 个 = 最粗。
        chain = sorted(ordered, key=lambda item: len(finer_than[item]))
        if [len(finer_than[item]) for item in chain] != list(range(len(chain))):
            return None
        # 只收敛到链的最粗端：细粒度独有证据在候选构造阶段就已把粗 scope
        # 排除，能走到多根歧义分支说明两端都可行——此时「没提明细」选粗是
        # 唯一无扇出的保守解释。最粗端必须是真正的事实分析对象（有业务指标，
        # 排除「客户范围」这类实体计数 scope——事实→实体的 many_to_one 与
        # 明细→主表在关系形态上不可区分，这是唯一的结构区分信号），且能
        # 覆盖全部精确证据；否则整体不适用，保持既有 fail-closed。
        coarsest = chain[0]
        dataset = self._datasets.get(coarsest)
        if dataset is None or not self._has_business_metric(coarsest):
            return None
        members = set(dataset.metric_ids) | set(dataset.dimension_ids)
        if evidence_element_ids <= members:
            return coarsest
        return None

    def _finish_scope_cardinality(
        self,
        *,
        candidates: set[str],
        selected_dataset_id: str | None,
        owners: set[str],
        exact_metric_ids: set[str],
        confirmed_metric_ids: set[str],
        ai_adjudicated_metric_ids: set[str],
        memory_confirmed_metric_ids: set[str],
        exact_dimension_ids: set[str],
        anchors: frozenset[str] = frozenset(),
    ) -> QueryScopeResolution:
        # Narrowing-only anchor contract: intersect and fall back to the
        # unanchored candidates when the intersection is empty, so semantic
        # evidence keeps every decision it makes today.
        applied_anchors: set[str] = set()
        if selected_dataset_id is None:
            narrowed = candidates.intersection(anchors)
            if narrowed:
                applied_anchors = narrowed
                candidates = narrowed
        if selected_dataset_id is not None:
            if selected_dataset_id not in candidates:
                return self._resolution(
                    status=QueryScopeResolutionStatus.REFUSED,
                    code="SELECTED_QUERY_SCOPE_INVALID",
                    message="确认的业务分析对象不再满足当前发布版本与精确语义证据。",
                    candidates=candidates,
                    owners=owners,
                    exact_metric_ids=exact_metric_ids,
                    confirmed_metric_ids=confirmed_metric_ids,
                    ai_adjudicated_metric_ids=ai_adjudicated_metric_ids,
                    memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                    exact_dimension_ids=exact_dimension_ids,
                )
            return self._resolution(
                status=QueryScopeResolutionStatus.SELECTED,
                code="QUERY_SCOPE_SELECTED",
                message="已确认受治理查询作用域。",
                selected=selected_dataset_id,
                candidates=candidates,
                owners=owners,
                exact_metric_ids=exact_metric_ids,
                confirmed_metric_ids=confirmed_metric_ids,
                ai_adjudicated_metric_ids=ai_adjudicated_metric_ids,
                memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                exact_dimension_ids=exact_dimension_ids,
            )
        if len(candidates) == 1:
            selected = next(iter(candidates))
            return self._resolution(
                status=QueryScopeResolutionStatus.SELECTED,
                code="QUERY_SCOPE_SELECTED",
                message=(
                    "作用域名称与精确语义证据唯一确定了受治理查询作用域。"
                    if applied_anchors
                    else "精确语义证据唯一确定了受治理查询作用域。"
                ),
                selected=selected,
                candidates=candidates,
                owners=owners,
                exact_metric_ids=exact_metric_ids,
                confirmed_metric_ids=confirmed_metric_ids,
                ai_adjudicated_metric_ids=ai_adjudicated_metric_ids,
                memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                exact_dimension_ids=exact_dimension_ids,
                anchors=applied_anchors,
            )
        converged = self._granularity_convergence(
            candidates, exact_metric_ids | exact_dimension_ids
        )
        if converged is not None:
            return self._resolution(
                status=QueryScopeResolutionStatus.SELECTED,
                code="QUERY_SCOPE_GRANULARITY_CONVERGED",
                message="候选作用域构成粒度从属链，已按覆盖全部精确语义证据的最粗粒度收敛。",
                selected=converged,
                candidates=candidates,
                owners=owners,
                exact_metric_ids=exact_metric_ids,
                confirmed_metric_ids=confirmed_metric_ids,
                ai_adjudicated_metric_ids=ai_adjudicated_metric_ids,
                memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                exact_dimension_ids=exact_dimension_ids,
                anchors=applied_anchors,
            )
        return self._resolution(
            status=QueryScopeResolutionStatus.CLARIFICATION,
            code="AMBIGUOUS_QUERY_SCOPE",
            message="多个受治理查询作用域满足当前精确语义证据。",
            candidates=candidates,
            owners=owners,
            exact_metric_ids=exact_metric_ids,
            confirmed_metric_ids=confirmed_metric_ids,
            ai_adjudicated_metric_ids=ai_adjudicated_metric_ids,
            memory_confirmed_metric_ids=memory_confirmed_metric_ids,
            exact_dimension_ids=exact_dimension_ids,
            anchors=applied_anchors,
        )

    def _allowed_dataset_ids(self, allowed_dataset_ids: Iterable[str] | None) -> set[str]:
        if allowed_dataset_ids is None:
            return set(self._datasets)
        return set(allowed_dataset_ids).intersection(self._datasets)

    def _is_current_evidence(self, evidence: QueryScopeEvidence) -> bool:
        element_type = _token(evidence.element_type)
        if element_type == "metric":
            return evidence.element_id in self._metrics
        if element_type == "dimension":
            return evidence.element_id in self._dimensions
        if element_type == "dimension_value":
            value = self._values.get(evidence.element_id)
            return value is not None and value.enabled and value.dimension_id in self._dimensions
        return False

    def _apply_selected_element(
        self,
        evidence: tuple[QueryScopeEvidence, ...],
        *,
        selected_element_id: str,
        selected_element_type: str,
    ) -> tuple[QueryScopeEvidence, ...]:
        selected_detected_texts = {
            _normalize_detected_text(item.detected_text)
            for item in evidence
            if item.element_id == selected_element_id
            and _token(item.element_type) == selected_element_type
        }
        return tuple(
            item
            for item in evidence
            if _normalize_detected_text(item.detected_text) not in selected_detected_texts
            or (
                item.element_id == selected_element_id
                and _token(item.element_type) == selected_element_type
            )
        )

    def _evidence_groups_fit_dataset(
        self,
        evidence: tuple[QueryScopeEvidence, ...],
        *,
        dataset_id: str,
    ) -> bool:
        grouped: dict[tuple[str, str], list[QueryScopeEvidence]] = defaultdict(list)
        for item in evidence:
            grouped[_evidence_group_key(item)].append(item)
        return all(
            any(dataset_id in self._evidence_scope_ids(item) for item in group)
            for group in grouped.values()
        )

    def _evidence_scope_ids(self, evidence: QueryScopeEvidence) -> set[str]:
        """Derive evidence visibility from governed membership, never legacy scope hints."""

        element_type = _token(evidence.element_type)
        element_id = evidence.element_id
        if element_type == "metric":
            if element_id not in self._metrics:
                return set()
            scope_ids = {
                dataset_id
                for dataset_id, dataset in self._datasets.items()
                if element_id in dataset.metric_ids
            }
        elif element_type == "dimension":
            if element_id not in self._dimensions:
                return set()
            dimension_id = element_id
        elif element_type == "dimension_value":
            value = self._values.get(element_id)
            if value is None or not value.enabled:
                return set()
            dimension_id = value.dimension_id
            if dimension_id not in self._dimensions:
                return set()
        else:
            return set()
        if element_type != "metric":
            scope_ids = {
                dataset_id
                for dataset_id, dataset in self._datasets.items()
                if dimension_id in dataset.dimension_ids
            }
        eligible_dataset_ids = getattr(evidence, "eligible_dataset_ids", None)
        if eligible_dataset_ids is not None:
            scope_ids.intersection_update(eligible_dataset_ids)
        return scope_ids

    def _dimension_reachable_scope_ids(
        self,
        *,
        dataset_ids: Iterable[str],
        dimension_groups: tuple[frozenset[str], ...],
    ) -> set[str]:
        return {
            dataset_id
            for dataset_id in dataset_ids
            if self._scope_reaches_dimension_groups(
                dataset_id=dataset_id,
                dimension_groups=dimension_groups,
            )
        }

    def _ambiguous_metric_scope_ids(
        self,
        *,
        allowed: set[str],
        metric_groups: tuple[frozenset[str], ...],
        dimension_groups: tuple[frozenset[str], ...],
    ) -> set[str]:
        return {
            dataset_id
            for dataset_id in allowed
            if dataset_id in self._routes
            and all(
                metric_group.intersection(self._datasets[dataset_id].metric_ids)
                for metric_group in metric_groups
            )
            and self._scope_reaches_dimension_groups(
                dataset_id=dataset_id,
                dimension_groups=dimension_groups,
            )
        }

    def _multi_fact_scope_ids(
        self,
        *,
        allowed: set[str],
        metric_ids: set[str],
        dimension_groups: tuple[frozenset[str], ...],
    ) -> set[str]:
        metrics_by_owner: dict[str, set[str]] = defaultdict(set)
        for metric_id in metric_ids:
            metrics_by_owner[self._metrics[metric_id].model_id].add(metric_id)
        return {
            dataset_id
            for dataset_id in allowed
            if (
                (route := self._routes.get(dataset_id)) is not None
                and metrics_by_owner[route.root_model_id].issubset(
                    self._datasets[dataset_id].metric_ids
                )
                and bool(metrics_by_owner[route.root_model_id])
                and self._scope_reaches_dimension_groups(
                    dataset_id=dataset_id,
                    dimension_groups=dimension_groups,
                )
            )
        }

    def _scope_reaches_dimension_groups(
        self,
        *,
        dataset_id: str,
        dimension_groups: tuple[frozenset[str], ...],
    ) -> bool:
        dataset = self._datasets[dataset_id]
        route = self._routes.get(dataset_id)
        if route is None:
            return False
        reachable_models = {
            route.root_model_id,
            *(path.target_model_id for path in route.paths),
        }
        return all(
            any(
                dimension_id in dataset.dimension_ids
                and self._dimensions[dimension_id].model_id in reachable_models
                for dimension_id in dimension_group
            )
            for dimension_group in dimension_groups
        )

    @staticmethod
    def _resolution(
        *,
        status: QueryScopeResolutionStatus,
        code: str,
        message: str,
        selected: str | None = None,
        candidates: Iterable[str] = (),
        owners: Iterable[str] = (),
        exact_metric_ids: Iterable[str] = (),
        confirmed_metric_ids: Iterable[str] = (),
        ai_adjudicated_metric_ids: Iterable[str] = (),
        memory_confirmed_metric_ids: Iterable[str] = (),
        exact_dimension_ids: Iterable[str] = (),
        ambiguities: Iterable[MetricPhraseAmbiguity] = (),
        anchors: Iterable[str] = (),
    ) -> QueryScopeResolution:
        return QueryScopeResolution(
            status=status,
            code=code,
            message=message,
            selected_dataset_id=selected,
            candidate_dataset_ids=tuple(sorted(set(candidates))),
            owner_model_ids=tuple(sorted(set(owners))),
            exact_metric_ids=tuple(sorted(set(exact_metric_ids))),
            confirmed_metric_ids=tuple(sorted(set(confirmed_metric_ids))),
            ai_adjudicated_metric_ids=tuple(sorted(set(ai_adjudicated_metric_ids))),
            memory_confirmed_metric_ids=tuple(sorted(set(memory_confirmed_metric_ids))),
            exact_dimension_ids=tuple(sorted(set(exact_dimension_ids))),
            ambiguous_metric_groups=tuple(ambiguities),
            anchor_dataset_ids=tuple(sorted(set(anchors))),
        )


def _token(value: object) -> str:
    return str(getattr(value, "value", value)).strip().casefold()


def _normalize_detected_text(value: str) -> str:
    # Match the mapper/index's stable NFKC + casefold + whitespace removal.  The
    # question is never read here; only the mapper-authored evidence boundary is.
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(normalized.split())


def _is_selectable_evidence(evidence: QueryScopeEvidence) -> bool:
    """A continuation token must point to evidence the Mapper actually recalled."""

    channel = _token(getattr(evidence, "channel", ""))
    return channel != "manifest" and _token(evidence.method) != "all_field"


def _is_exact_direct_evidence(evidence: QueryScopeEvidence) -> bool:
    """Return the frozen Scope-routing evidence class, excluding Term remaps."""

    if _token(evidence.method) != "exact":
        return False
    if getattr(evidence, "origin_term_entry_id", None) is not None:
        return False
    return _token(getattr(evidence, "channel", "")) not in {
        "term_dictionary",
        "term_database",
        "term_embedding",
        "manifest",
    }


def _evidence_group_key(evidence: QueryScopeEvidence) -> tuple[str, str]:
    element_type = _token(evidence.element_type)
    semantic_group = "metric" if element_type == "metric" else "dimension"
    return semantic_group, _normalize_detected_text(evidence.detected_text)


def _index_by_id[IdentifiedT: _Identified](
    items: Iterable[IdentifiedT], *, label: str
) -> dict[str, IdentifiedT]:
    indexed: dict[str, IdentifiedT] = {}
    for item in items:
        item_id = str(item.id)
        if item_id in indexed:
            raise ValueError(f"duplicate {label} id: {item_id}")
        indexed[item_id] = item
    return indexed


def _index_by_dataset_id(
    routes: Iterable[AnalysisTopicRouteSpec],
) -> dict[str, AnalysisTopicRouteSpec]:
    indexed: dict[str, AnalysisTopicRouteSpec] = {}
    for route in routes:
        if route.dataset_id in indexed:
            raise ValueError(f"duplicate query scope route: {route.dataset_id}")
        indexed[route.dataset_id] = route
    return indexed
