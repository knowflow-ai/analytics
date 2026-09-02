from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from knowflow_analytics.query.contracts import (
    MapMode,
    MappingConfig,
    MappingEvidence,
    MappingEvidenceChannel,
    MappingEvidenceMatch,
    MappingResult,
    MatchMethod,
    SchemaMatch,
    SemanticAmbiguityGroup,
    SemanticAmbiguityMember,
)
from knowflow_analytics.query.exemplars import relevance_score
from knowflow_analytics.query.hanlp import HanlpCustomDictionary
from knowflow_analytics.semantic.index import (
    EmbeddingGateway,
    IndexState,
    SemanticElementType,
    SemanticIndexEntry,
    SemanticIndexSnapshot,
    normalize_text,
)

_MATCH_METHOD_PRIORITY = {
    MatchMethod.ALL_FIELD: 0,
    MatchMethod.EMBEDDING: 1,
    MatchMethod.KEYWORD: 2,
    MatchMethod.TERM: 3,
    MatchMethod.EXACT: 4,
}
_COVERING_MATCH_METHODS = frozenset(
    {
        MatchMethod.EXACT,
        MatchMethod.KEYWORD,
        MatchMethod.TERM,
        MatchMethod.EMBEDDING,
    }
)
_PARTIAL_MATCH_METHODS = frozenset({MatchMethod.KEYWORD, MatchMethod.EMBEDDING})


def recall_order_key(phrase: str) -> tuple[int, str]:
    """前后缀召回在截断前的排序键。

    上游 SearchService.java:63/98 的比较器是
    ``(a, b) -> -(b.getName().length() - a.getName().length())``,展开即
    ``a.len - b.len``——**按名称长度升序,最短优先**,再 .limit(detection_max_size)。
    短名与探测片段的编辑距离最近、相似度最高,应当优先保留;我们此前按最长优先,
    等于在相似度过滤之前先丢掉最可能正确的候选。

    字典大到触发截断时才有区别,且主名维度/指标另有子串召回路径兜底,
    实际受影响的是别名与维度值条目。字典序只作稳定化,不引入语义偏好。
    """

    return len(phrase), phrase


class SemanticMapper:
    """Release-scoped mapper registry."""

    def __init__(
        self,
        *,
        embedding_gateway: EmbeddingGateway | None = None,
        config: MappingConfig | None = None,
        llm_enabled: bool = False,
    ) -> None:
        self._embedding_gateway = embedding_gateway
        self.config = config or MappingConfig()
        # Parity source: ``EmbeddingMapper.accept`` is
        # ``MapModeEnum.LOOSE || Text2SQLType == LLM_OR_RULE``. A deployment that
        # enables the LLM parser therefore recalls embeddings in every mapping
        # round, not only the final one.
        self._llm_enabled = llm_enabled

    def map(
        self,
        *,
        question: str,
        dataset_id: str,
        index: SemanticIndexSnapshot,
        mode: MapMode,
        selected_element_id: str | None = None,
        selected_element_type: SemanticElementType | None = None,
        final_stage: bool = False,
        tenant_id: str = "",
        allowed_element_ids: frozenset[str] | None = None,
    ) -> MappingResult:
        """Compatibility projection for one Scope.

        New multi-Scope callers collect once with :meth:`collect_evidence` and
        project every mode with :meth:`project_evidence`.  Keeping this method as
        a thin adapter preserves the existing Mapper contract for single-Scope
        callers and tests.
        """

        include_embeddings = mode is MapMode.LOOSE or final_stage or self._llm_enabled
        evidence = self.collect_evidence(
            question=question,
            dataset_ids=(dataset_id,),
            index=index,
            include_embeddings=include_embeddings,
            tenant_id=tenant_id,
            allowed_element_ids=allowed_element_ids,
        )
        return self.project_evidence(
            evidence=evidence,
            dataset_id=dataset_id,
            mode=mode,
            selected_element_id=selected_element_id,
            selected_element_type=selected_element_type,
            final_stage=final_stage,
        )

    def collect_evidence(
        self,
        *,
        question: str,
        dataset_ids: tuple[str, ...],
        index: SemanticIndexSnapshot,
        include_embeddings: bool = True,
        tenant_id: str = "",
        allowed_element_ids: frozenset[str] | None = None,
    ) -> MappingEvidence:
        """Retrieve raw evidence once across the allowed QueryScope union.

        This is the CANDIDATE_DISCOVERY Mapper stage only.  It does not choose a
        Scope or infer query meaning.  Per-Scope thresholds, top-k, deduplication,
        ambiguity groups and ALL manifest completion remain projection concerns.
        """

        if tenant_id and self._embedding_gateway is not None:
            # Mapper instances are application-wide, but model ownership is
            # request-scoped. Build a lightweight tenant view so every embedding
            # round, including term remapping, uses the signed-in tenant.
            return type(self)(
                embedding_gateway=self._embedding_gateway.for_tenant(tenant_id),
                config=self.config,
                llm_enabled=self._llm_enabled,
            ).collect_evidence(
                question=question,
                dataset_ids=dataset_ids,
                index=index,
                include_embeddings=include_embeddings,
                allowed_element_ids=allowed_element_ids,
            )
        if index.state is not IndexState.READY:
            raise ValueError("semantic index must be READY")

        allowed_dataset_ids = tuple(sorted(set(dataset_ids)))
        allowed = set(allowed_dataset_ids)
        entries = tuple(entry for entry in index.entries if allowed.intersection(entry.dataset_ids))
        if allowed_element_ids is not None:
            # 列级权限：不可见成员在**检索之前**就被移出候选，而不是在结果处过滤。
            # 放在这里是因为关键词、术语、Embedding 三条通道都在这一行之后消费
            # `entries`，一处过滤即全部覆盖；放到投影或结果层则会让不可见成员的
            # 名字先出现在澄清卡、联想和下钻候选里——名称泄漏比数据泄漏更隐蔽。
            #
            # 索引本身仍是全量的一份：绝不按用户裁剪索引（那会变成每人一份，
            # 成本与一致性双崩），只在消费侧收窄。
            #
            # 维度值条目挂在其所属维度上（element_id 是维度值自己的 ID），用
            # dimension_id 判可见性；维度不可见时它的取值也不该被检索到。
            #
            # DATASET 条目不受成员白名单管辖：作用域名是确定性 Scope 锚点，它的
            # 可见性已经由 dataset_ids 授权决定；列级权限约束的是**授权范围内的
            # 成员**，不是再切一层作用域。把它一起过滤掉会让有列限制的用户失去
            # 逐字点名作用域的能力。
            entries = tuple(
                entry
                for entry in entries
                if entry.element_type is SemanticElementType.DATASET
                or (entry.dimension_id or entry.element_id) in allowed_element_ids
            )
        normalized_question = normalize_text(question)

        raw_matches = self._collect_keyword_evidence(
            text=question,
            entries=entries,
            eligible_dataset_ids=allowed_dataset_ids,
            origin_term_entry_id=None,
        )

        entries_by_id = {entry.id: entry for entry in entries}
        exact_term_scopes: dict[str, set[str]] = defaultdict(set)
        for item in raw_matches:
            if (
                item.channel is MappingEvidenceChannel.DICTIONARY
                and item.element_type is SemanticElementType.TERM
                and item.score == 1.0
            ):
                exact_term_scopes[item.entry_id].update(item.eligible_dataset_ids)

        for term_entry_id in sorted(exact_term_scopes):
            term = entries_by_id[term_entry_id]
            if not term.description.strip():
                continue
            raw_matches.extend(
                self._collect_keyword_evidence(
                    text=term.description,
                    entries=entries,
                    eligible_dataset_ids=tuple(sorted(exact_term_scopes[term_entry_id])),
                    origin_term_entry_id=term_entry_id,
                )
            )

        for entry in entries:
            eligible = self._eligible_dataset_ids(
                entry.dataset_ids,
                allowed_dataset_ids,
            )
            raw_matches.append(
                self._evidence_match(
                    entry,
                    eligible_dataset_ids=eligible,
                    channel=MappingEvidenceChannel.MANIFEST,
                    method=MatchMethod.ALL_FIELD,
                    score=0.1,
                    detected_text=entry.phrase,
                )
            )

        embedding_collected = include_embeddings and self._embedding_gateway is not None
        if embedding_collected:
            raw_matches.extend(
                self._collect_embedding_evidence(
                    question=question,
                    entries=entries,
                    allowed_dataset_ids=allowed_dataset_ids,
                    exact_term_scopes=exact_term_scopes,
                    entries_by_id=entries_by_id,
                    index=index,
                )
            )

        # Contract: CANDIDATE_DISCOVERY performs one global longest-surface
        # decision before Scope projection. A partial keyword nested entirely
        # inside a longer exact/lexical/embedding segment is one user phrase,
        # even when the two semantic candidates belong to different Scopes.
        raw_matches = _filter_global_surface_evidence(raw_matches)

        return MappingEvidence(
            normalized_question=normalized_question,
            dataset_ids=allowed_dataset_ids,
            matches=tuple(raw_matches),
            config_version=self.config.version,
            index_snapshot_id=index.id,
            embedding_model_id=index.embedding_model_id,
            embedding_collected=embedding_collected,
            embedding_gateway_available=self._embedding_gateway is not None,
        )

    def project_evidence(
        self,
        *,
        evidence: MappingEvidence,
        dataset_id: str,
        mode: MapMode,
        selected_element_id: str | None = None,
        selected_element_type: SemanticElementType | None = None,
        final_stage: bool = False,
    ) -> MappingResult:
        """Deterministically project global evidence into the legacy result view."""

        if dataset_id not in evidence.dataset_ids:
            raise ValueError("dataset is outside collected mapping evidence")
        if evidence.config_version != self.config.version:
            raise ValueError("mapping evidence config differs from mapper config")

        scoped = tuple(item for item in evidence.matches if dataset_id in item.eligible_dataset_ids)
        direct = tuple(item for item in scoped if item.origin_term_entry_id is None)
        embedding_enabled = mode is MapMode.LOOSE or final_stage or self._llm_enabled
        degraded: list[str] = []

        matches: list[SchemaMatch] = []
        if embedding_enabled:
            if evidence.embedding_collected:
                matches.extend(
                    self._project_embedding_evidence(
                        direct,
                        dataset_id=dataset_id,
                        channel=MappingEvidenceChannel.EMBEDDING,
                    )
                )
            else:
                degraded.append("embedding_gateway_unavailable")

        dictionary = self._project_dictionary_evidence(
            direct,
            dataset_id=dataset_id,
            mode=mode,
            channel=MappingEvidenceChannel.DICTIONARY,
        )
        matches.extend(dictionary)
        matches.extend(
            self._project_database_evidence(
                direct,
                dataset_id=dataset_id,
                mode=mode,
                channel=MappingEvidenceChannel.DATABASE,
                existing_matches=list(matches),
            )
        )

        if mode is MapMode.ALL:
            matches.extend(
                self._schema_match(item, dataset_id=dataset_id)
                for item in direct
                if item.channel is MappingEvidenceChannel.MANIFEST
                and item.element_type in {SemanticElementType.METRIC, SemanticElementType.DIMENSION}
            )

        deduplicated = self._apply_map_filters(self._deduplicate(matches))
        expanded = self._project_term_evidence(
            evidence=scoped,
            matches=deduplicated,
            dataset_id=dataset_id,
            mode=mode,
            embedding_enabled=embedding_enabled and evidence.embedding_collected,
        )
        deduplicated = self._apply_map_filters(self._deduplicate([*deduplicated, *expanded]))
        if selected_element_id is not None:
            deduplicated = self._apply_element_selection(
                deduplicated,
                scoped=scoped,
                dataset_id=dataset_id,
                selected_element_id=selected_element_id,
                selected_element_type=selected_element_type,
            )
        semantic_ambiguities = self._ambiguities(deduplicated)
        return MappingResult(
            dataset_id=dataset_id,
            mode=mode,
            normalized_question=evidence.normalized_question,
            matches=tuple(deduplicated),
            ambiguous_groups=tuple(
                tuple(member.element_id for member in group.members)
                for group in semantic_ambiguities
            ),
            semantic_ambiguity_groups=semantic_ambiguities,
            config_version=self.config.version,
            degraded_reasons=tuple(degraded),
        )

    def _collect_keyword_evidence(
        self,
        *,
        text: str,
        entries: tuple[SemanticIndexEntry, ...],
        eligible_dataset_ids: tuple[str, ...],
        origin_term_entry_id: str | None,
    ) -> list[MappingEvidenceMatch]:
        """Collect uncapped lexical evidence; Scope/mode filtering happens later."""

        segment_scopes = self._dictionary_segment_scopes(
            text=text,
            entries=entries,
            eligible_dataset_ids=eligible_dataset_ids,
        )
        dictionary_channel = (
            MappingEvidenceChannel.TERM_DICTIONARY
            if origin_term_entry_id is not None
            else MappingEvidenceChannel.DICTIONARY
        )
        database_channel = (
            MappingEvidenceChannel.TERM_DATABASE
            if origin_term_entry_id is not None
            else MappingEvidenceChannel.DATABASE
        )
        matches: list[MappingEvidenceMatch] = []
        for detected_text, (segment_dataset_ids, detected_spans) in segment_scopes.items():
            if not detected_text:
                continue
            for entry in entries:
                eligible = self._eligible_dataset_ids(
                    entry.dataset_ids,
                    segment_dataset_ids,
                    eligible_dataset_ids,
                )
                if not eligible:
                    continue
                if entry.normalized_phrase.startswith(
                    detected_text
                ) or entry.normalized_phrase.endswith(detected_text):
                    score = _edit_similarity(entry.normalized_phrase, detected_text)
                    matches.append(
                        self._evidence_match(
                            entry,
                            eligible_dataset_ids=eligible,
                            channel=dictionary_channel,
                            method=(MatchMethod.EXACT if score == 1.0 else MatchMethod.KEYWORD),
                            score=score,
                            detected_text=detected_text,
                            origin_term_entry_id=origin_term_entry_id,
                            detected_spans=detected_spans,
                        )
                    )
                if (
                    entry.source == "name"
                    and entry.element_type
                    in {SemanticElementType.METRIC, SemanticElementType.DIMENSION}
                    and detected_text in entry.normalized_phrase
                ):
                    matches.append(
                        self._evidence_match(
                            entry,
                            eligible_dataset_ids=eligible,
                            channel=database_channel,
                            method=MatchMethod.KEYWORD,
                            score=_edit_similarity(detected_text, entry.normalized_phrase),
                            detected_text=detected_text,
                            origin_term_entry_id=origin_term_entry_id,
                            detected_spans=detected_spans,
                        )
                    )
        return matches

    @staticmethod
    def _dictionary_segment_scopes(
        *,
        text: str,
        entries: tuple[SemanticIndexEntry, ...],
        eligible_dataset_ids: tuple[str, ...],
    ) -> dict[str, tuple[tuple[str, ...], tuple[tuple[int, int], ...]]]:
        """Run one global longest-word scan, then attach Scope eligibility.

        A Scope-local scan can interpret one phrase twice: the merchant Scope
        sees ``入驻电商数量`` while the platform Scope restarts inside it at
        ``电商数量``.  Those are not two user intents.  Global segmentation is
        the authority for detected spans; entry membership is intersected later
        when evidence is projected into a Scope.
        """

        return {
            segment: (eligible_dataset_ids, spans)
            for segment, spans in _dictionary_segment_spans(text, entries).items()
        }

    def _collect_embedding_evidence(
        self,
        *,
        question: str,
        entries: tuple[SemanticIndexEntry, ...],
        allowed_dataset_ids: tuple[str, ...],
        exact_term_scopes: dict[str, set[str]],
        entries_by_id: dict[str, SemanticIndexEntry],
        index: SemanticIndexSnapshot,
    ) -> list[MappingEvidenceMatch]:
        """Batch question and exact-term-description segments in one model call."""

        assert self._embedding_gateway is not None
        requests: list[tuple[str | None, str, tuple[str, ...], tuple[tuple[int, int], ...]]] = [
            (None, segment, allowed_dataset_ids, spans)
            for segment, spans in _embedding_segments_with_spans(question, entries)
        ]
        for term_entry_id in sorted(exact_term_scopes):
            term = entries_by_id[term_entry_id]
            if not term.description.strip():
                continue
            term_dataset_ids = tuple(sorted(exact_term_scopes[term_entry_id]))
            requests.extend(
                (
                    term_entry_id,
                    segment,
                    term_dataset_ids,
                    spans,
                )
                for segment, spans in _embedding_segments_with_spans(
                    term.description,
                    entries,
                )
            )

        batch = self._embedding_gateway.encode(tuple(item[1] for item in requests))
        if batch.model_id != index.embedding_model_id:
            raise ValueError("query embedding model differs from index snapshot")
        if batch.dimension != index.vector_dimension or len(batch.vectors) != len(requests):
            raise ValueError("query embedding dimension differs from index snapshot")

        vector_by_id = {
            entry.id: vector for entry, vector in zip(index.entries, index.vectors, strict=True)
        }
        matches: list[MappingEvidenceMatch] = []
        for (
            origin_term_entry_id,
            segment,
            request_dataset_ids,
            detected_spans,
        ), query_vector in zip(
            requests,
            batch.vectors,
            strict=True,
        ):
            channel = (
                MappingEvidenceChannel.TERM_EMBEDDING
                if origin_term_entry_id is not None
                else MappingEvidenceChannel.EMBEDDING
            )
            normalized_segment = normalize_text(segment)
            if len(normalized_segment) <= 1:
                continue
            for entry in entries:
                if entry.element_type is SemanticElementType.DATASET:
                    continue
                eligible = self._eligible_dataset_ids(
                    entry.dataset_ids,
                    request_dataset_ids,
                    allowed_dataset_ids,
                )
                if not eligible:
                    continue
                score = _cosine(query_vector, vector_by_id[entry.id])
                exact_span = _contains(normalized_segment, entry.normalized_phrase)
                if not exact_span and relevance_score(score) < self.config.embedding_similarity:
                    continue
                if len(entry.normalized_phrase) <= 2 and score < 1.0 and not exact_span:
                    continue
                matches.append(
                    self._evidence_match(
                        entry,
                        eligible_dataset_ids=eligible,
                        channel=channel,
                        method=MatchMethod.EMBEDDING,
                        score=score,
                        detected_text=segment,
                        origin_term_entry_id=origin_term_entry_id,
                        detected_spans=detected_spans,
                    )
                )
        return matches

    def _project_dictionary_evidence(
        self,
        evidence: tuple[MappingEvidenceMatch, ...],
        *,
        dataset_id: str,
        mode: MapMode,
        channel: MappingEvidenceChannel,
    ) -> list[SchemaMatch]:
        raw = [item for item in evidence if item.channel is channel]
        by_detected: dict[str, list[MappingEvidenceMatch]] = defaultdict(list)
        for item in raw:
            by_detected[item.detected_text].append(item)

        matches: list[SchemaMatch] = []
        for detected_text, detected_items in by_detected.items():
            by_phrase: dict[str, list[MappingEvidenceMatch]] = defaultdict(list)
            for item in detected_items:
                by_phrase[item.normalized_phrase].append(item)

            prefix = sorted(
                (
                    phrase
                    for phrase in by_phrase
                    if phrase.startswith(normalize_text(detected_text))
                ),
                key=recall_order_key,
            )[: self.config.detection_max_size]
            suffix = sorted(
                (phrase for phrase in by_phrase if phrase.endswith(normalize_text(detected_text))),
                key=recall_order_key,
            )[: self.config.detection_max_size]
            recalled = tuple(dict.fromkeys((*prefix, *suffix)))
            ranked = sorted(
                (
                    (
                        phrase,
                        _edit_similarity(phrase, normalize_text(detected_text)),
                    )
                    for phrase in recalled
                ),
                key=lambda item: (-item[1], -len(item[0]), item[0]),
            )

            eligible: list[tuple[str, float, bool]] = []
            for phrase, score in ranked:
                phrase_items = by_phrase[phrase]
                value_item = next(
                    (
                        item
                        for item in phrase_items
                        if item.element_type is SemanticElementType.DIMENSION_VALUE
                    ),
                    None,
                )
                threshold_item = value_item or phrase_items[0]
                if score >= self._keyword_threshold(threshold_item, mode):
                    eligible.append((phrase, score, value_item is not None))

            values = [item for item in eligible if item[2]][: self.config.dimension_value_size]
            remaining = max(self.config.detection_size - len(values), 0)
            non_values = [item for item in eligible if not item[2]][:remaining]
            for phrase, score, _is_value in (*values, *non_values):
                for item in sorted(
                    by_phrase[phrase],
                    key=lambda value: (value.element_type, value.element_id, value.entry_id),
                ):
                    matches.append(
                        self._schema_match(
                            item.model_copy(
                                update={
                                    "method": (
                                        MatchMethod.EXACT if score == 1.0 else MatchMethod.KEYWORD
                                    ),
                                    "score": score,
                                }
                            ),
                            dataset_id=dataset_id,
                        )
                    )
        return matches

    def _project_database_evidence(
        self,
        evidence: tuple[MappingEvidenceMatch, ...],
        *,
        dataset_id: str,
        mode: MapMode,
        channel: MappingEvidenceChannel,
        existing_matches: list[SchemaMatch],
    ) -> list[SchemaMatch]:
        registered = {
            (item.element_type, item.element_id)
            for item in existing_matches
            if item.element_type in {SemanticElementType.METRIC, SemanticElementType.DIMENSION}
        }
        threshold = self._database_name_threshold(
            mode=mode,
            has_registered_element=bool(existing_matches),
        )
        raw = sorted(
            (item for item in evidence if item.channel is channel),
            key=lambda item: (item.detected_text, item.entry_id),
        )
        return [
            self._schema_match(item, dataset_id=dataset_id)
            for item in raw
            if (item.element_type, item.element_id) not in registered and item.score >= threshold
        ]

    def _project_embedding_evidence(
        self,
        evidence: tuple[MappingEvidenceMatch, ...],
        *,
        dataset_id: str,
        channel: MappingEvidenceChannel,
    ) -> list[SchemaMatch]:
        by_segment: dict[str, list[MappingEvidenceMatch]] = defaultdict(list)
        for item in evidence:
            if item.channel is channel:
                by_segment[item.detected_text].append(item)

        matches = [
            self._schema_match(item, dataset_id=dataset_id)
            for segment_items in by_segment.values()
            for item in sorted(
                segment_items,
                key=lambda value: (-value.score, -value.priority, value.entry_id),
            )[:5]
        ]
        return sorted(
            matches,
            key=lambda item: (-item.score, -item.priority, item.element_type, item.element_id),
        )[: 10 * len(by_segment)]

    def _project_term_evidence(
        self,
        *,
        evidence: tuple[MappingEvidenceMatch, ...],
        matches: list[SchemaMatch],
        dataset_id: str,
        mode: MapMode,
        embedding_enabled: bool,
    ) -> list[SchemaMatch]:
        expanded: list[SchemaMatch] = []
        for match in matches:
            if match.element_type is not SemanticElementType.TERM or match.score != 1.0:
                continue
            term_evidence = tuple(
                item for item in evidence if item.origin_term_entry_id == match.entry_id
            )
            dictionary = self._project_dictionary_evidence(
                term_evidence,
                dataset_id=dataset_id,
                mode=mode,
                channel=MappingEvidenceChannel.TERM_DICTIONARY,
            )
            mapped = [
                *dictionary,
                *self._project_database_evidence(
                    term_evidence,
                    dataset_id=dataset_id,
                    mode=mode,
                    channel=MappingEvidenceChannel.TERM_DATABASE,
                    existing_matches=[*matches, *dictionary],
                ),
            ]
            if embedding_enabled:
                mapped.extend(
                    self._project_embedding_evidence(
                        term_evidence,
                        dataset_id=dataset_id,
                        channel=MappingEvidenceChannel.TERM_EMBEDDING,
                    )
                )
            expanded.extend(
                item.model_copy(update={"method": MatchMethod.TERM})
                for item in mapped
                if item.element_type is not SemanticElementType.TERM
            )
        return expanded

    @staticmethod
    def _eligible_dataset_ids(*dataset_id_groups: tuple[str, ...]) -> tuple[str, ...]:
        if not dataset_id_groups:
            return ()
        eligible = set(dataset_id_groups[0])
        for group in dataset_id_groups[1:]:
            eligible.intersection_update(group)
        return tuple(sorted(eligible))

    @staticmethod
    def _evidence_match(
        entry: SemanticIndexEntry,
        *,
        eligible_dataset_ids: tuple[str, ...],
        channel: MappingEvidenceChannel,
        method: MatchMethod,
        score: float,
        detected_text: str,
        origin_term_entry_id: str | None = None,
        detected_spans: tuple[tuple[int, int], ...] = (),
    ) -> MappingEvidenceMatch:
        # Pydantic defensively rebuilds every nested tuple during validation.
        # One detected surface can fan out to hundreds of semantic candidates,
        # so copying the same (potentially large) occurrence list into every
        # evidence object makes memory grow with candidates * occurrences.
        # Validate the public model with its default empty tuple, then attach the
        # mapper-owned immutable tuple by reference.  The tuple is produced by
        # the coordinate helpers below and is never mutated.
        match = MappingEvidenceMatch(
            entry_id=entry.id,
            eligible_dataset_ids=eligible_dataset_ids,
            element_type=entry.element_type,
            element_id=entry.element_id,
            phrase=entry.phrase,
            normalized_phrase=entry.normalized_phrase,
            detected_text=detected_text,
            method=method,
            # Floating-point cosine can exceed one by a few ulps. SchemaMatch
            # already clamps this boundary; scope-neutral evidence must keep
            # the same governed score contract.
            score=max(-1.0, min(score, 1.0)),
            priority=entry.priority,
            channel=channel,
            entry_source=entry.source,
            description=entry.description,
            dimension_id=entry.dimension_id,
            raw_value=entry.raw_value,
            origin_term_entry_id=origin_term_entry_id,
            detected_span_source=(
                f"term:{origin_term_entry_id}" if origin_term_entry_id is not None else "question"
            ),
        )
        if not detected_spans:
            return match
        return match.model_copy(update={"detected_spans": detected_spans})

    @staticmethod
    def _schema_match(item: MappingEvidenceMatch, *, dataset_id: str) -> SchemaMatch:
        match = SchemaMatch(
            entry_id=item.entry_id,
            dataset_id=dataset_id,
            element_type=item.element_type,
            element_id=item.element_id,
            phrase=item.phrase,
            detected_text=item.detected_text,
            method=item.method,
            score=max(0.0, min(item.score, 1.0)),
            priority=item.priority,
            dimension_id=item.dimension_id,
            raw_value=item.raw_value,
            detected_span_source=item.detected_span_source,
        )
        if not item.detected_spans:
            return match
        return match.model_copy(update={"detected_spans": item.detected_spans})

    def _apply_element_selection(
        self,
        matches: list[SchemaMatch],
        *,
        scoped: tuple[MappingEvidenceMatch, ...],
        dataset_id: str,
        selected_element_id: str,
        selected_element_type: SemanticElementType | None = None,
    ) -> list[SchemaMatch]:
        eligible_evidence = [
            item
            for item in scoped
            if item.element_id == selected_element_id
            and (selected_element_type is None or item.element_type is selected_element_type)
            and item.element_type
            in {
                SemanticElementType.METRIC,
                SemanticElementType.DIMENSION,
                SemanticElementType.DIMENSION_VALUE,
            }
            and item.channel is not MappingEvidenceChannel.MANIFEST
            and item.method is not MatchMethod.ALL_FIELD
        ]
        if not eligible_evidence:
            return matches
        selected_evidence = max(
            eligible_evidence,
            key=lambda item: (
                _MATCH_METHOD_PRIORITY[item.method],
                item.score,
                item.priority,
                len(item.phrase),
                item.entry_id,
            ),
        )
        selected_key = (selected_evidence.element_type, selected_evidence.element_id)
        projected_selected = [
            item for item in matches if (item.element_type, item.element_id) == selected_key
        ]
        selected_detected = {
            normalize_text(item.detected_text or item.phrase) for item in projected_selected
        }
        if not selected_detected:
            selected_detected.add(
                normalize_text(selected_evidence.detected_text or selected_evidence.phrase)
            )
        competing_keys = {
            (item.element_type, item.element_id)
            for item in matches
            if normalize_text(item.detected_text or item.phrase) in selected_detected
            and (item.element_type, item.element_id) != selected_key
        }
        selected_match = (
            max(
                projected_selected,
                key=lambda item: (
                    _MATCH_METHOD_PRIORITY[item.method],
                    item.score,
                    item.priority,
                    len(item.phrase),
                    item.entry_id,
                ),
            )
            if projected_selected
            else self._schema_match(selected_evidence, dataset_id=dataset_id).model_copy(
                update={"method": MatchMethod.TERM}
                if selected_evidence.origin_term_entry_id is not None
                else {}
            )
        )
        return self._deduplicate(
            [
                item
                for item in matches
                if (item.element_type, item.element_id) not in competing_keys
                and (item.element_type, item.element_id) != selected_key
            ]
            + [selected_match]
        )

    def _keyword_matches(
        self,
        *,
        question: str,
        normalized_question: str,
        dataset_id: str,
        entries: tuple[SemanticIndexEntry, ...],
        mode: MapMode,
        existing_matches: list[SchemaMatch],
    ) -> list[SchemaMatch]:
        """Match governed words by segmentation plus edit distance.

        Parity sources:
        ``SingleMatchStrategy.java`` enumerates query segments;
        ``SearchService.java`` performs prefix/suffix recall;
        ``HanlpDictMatchStrategy.java`` caps each search at 20 names and every
        detection round at eight names, including at most one dimension value;
        ``EditDistanceUtils.java`` supplies the normalized Damerau-Levenshtein score.
        This intentionally does not compare every schema name with the whole question
        using ``partial_ratio``.
        """

        matches: list[SchemaMatch] = []
        # Parity source: SingleMatchStrategy + MapperHelper. HanLP registered
        # semantic words are atomic scan units: once one governed phrase is
        # registered at an offset, the scanner must not restart from an inner
        # substring and create a second fuzzy semantic match.
        segments = _dictionary_segments(
            question,
            entries,
        )
        entries_by_phrase: dict[str, list[SemanticIndexEntry]] = defaultdict(list)
        for entry in entries:
            entries_by_phrase[entry.normalized_phrase].append(entry)

        dictionary_matches: list[SchemaMatch] = []
        for detected_text in segments:
            selected = self._select_dictionary_round(
                detected_text=detected_text,
                entries_by_phrase=entries_by_phrase,
                mode=mode,
            )
            for normalized_phrase, score in selected:
                method = MatchMethod.EXACT if score == 1.0 else MatchMethod.KEYWORD
                for entry in sorted(
                    entries_by_phrase[normalized_phrase],
                    key=lambda item: (item.element_type, item.element_id, item.id),
                ):
                    dictionary_matches.append(
                        self._match(
                            entry,
                            dataset_id=dataset_id,
                            method=method,
                            score=score,
                            detected_text=detected_text,
                        )
                    )
        matches.extend(dictionary_matches)
        matches.extend(
            self._database_name_matches(
                segments=segments,
                entries=entries,
                dataset_id=dataset_id,
                mode=mode,
                # 上游读的是整个 mapInfo:此时 EmbeddingMapper 已先写入匹配。
                existing_matches=[*existing_matches, *dictionary_matches],
            )
        )
        return matches

    def _database_name_matches(
        self,
        *,
        segments: tuple[str, ...],
        entries: tuple[SemanticIndexEntry, ...],
        dataset_id: str,
        mode: MapMode,
        existing_matches: list[SchemaMatch],
    ) -> list[SchemaMatch]:
        """Keyword mapping backed by the governed database match strategy.

        The database strategy runs after the HanLP dictionary strategy, scans only
        the primary names of metrics and dimensions, and adds an element only when
        its name contains the detected query segment and passes the configured edit
        similarity threshold.  It never expands aliases, values, terms or datasets.
        """

        registered = {
            item.element_id
            for item in existing_matches
            if item.element_type in {SemanticElementType.METRIC, SemanticElementType.DIMENSION}
        }
        # Parity source: DatabaseMatchStrategy.getThreshold checks whether the
        # existing SchemaMap contains any element, not only a metric/dimension.
        # A governed dimension-value hit therefore also prevents the threshold
        # from being relaxed for the following database-name pass.
        has_registered_element = bool(existing_matches)
        threshold = self._database_name_threshold(
            mode=mode,
            has_registered_element=has_registered_element,
        )
        primary_names = tuple(
            entry
            for entry in entries
            if entry.source == "name"
            and entry.element_type in {SemanticElementType.METRIC, SemanticElementType.DIMENSION}
        )
        results: list[SchemaMatch] = []
        for detected_text in segments:
            if not detected_text:
                continue
            for entry in primary_names:
                if entry.element_id in registered:
                    continue
                if detected_text not in entry.normalized_phrase:
                    continue
                score = _edit_similarity(detected_text, entry.normalized_phrase)
                if score < threshold:
                    continue
                results.append(
                    self._match(
                        entry,
                        dataset_id=dataset_id,
                        method=MatchMethod.KEYWORD,
                        score=score,
                        detected_text=detected_text,
                    )
                )
        return results

    def _database_name_threshold(
        self,
        *,
        mode: MapMode,
        has_registered_element: bool,
    ) -> float:
        if mode is MapMode.STRICT:
            return 1.0
        configured = self.config.name_similarity
        if not has_registered_element:
            configured /= 2
        minimum = self.config.name_min_similarity
        mode_step = {MapMode.MODERATE: 2, MapMode.LOOSE: 4, MapMode.ALL: 6}[mode]
        decrease = (configured - minimum) / 4
        return max(configured - mode_step * decrease, minimum)

    def _select_dictionary_round(
        self,
        *,
        detected_text: str,
        entries_by_phrase: dict[str, list[SemanticIndexEntry]],
        mode: MapMode,
    ) -> list[tuple[str, float]]:
        """Apply ``HanlpDictMatchStrategy.detectByStep`` to one text segment."""

        if not detected_text:
            return []

        def search_key(phrase: str) -> tuple[int, str]:
            return recall_order_key(phrase)

        prefix = sorted(
            (phrase for phrase in entries_by_phrase if phrase.startswith(detected_text)),
            key=search_key,
        )[: self.config.detection_max_size]
        suffix = sorted(
            (phrase for phrase in entries_by_phrase if phrase.endswith(detected_text)),
            key=search_key,
        )[: self.config.detection_max_size]
        recalled = tuple(dict.fromkeys((*prefix, *suffix)))
        ranked = sorted(
            ((phrase, _edit_similarity(phrase, detected_text)) for phrase in recalled),
            key=lambda item: (-item[1], -len(item[0]), item[0]),
        )

        eligible: list[tuple[str, float, bool]] = []
        for phrase, score in ranked:
            phrase_entries = entries_by_phrase[phrase]
            is_value = any(
                entry.element_type is SemanticElementType.DIMENSION_VALUE
                for entry in phrase_entries
            )
            threshold_entry = next(
                (
                    entry
                    for entry in phrase_entries
                    if entry.element_type is SemanticElementType.DIMENSION_VALUE
                ),
                phrase_entries[0],
            )
            if score >= self._keyword_threshold(threshold_entry, mode):
                eligible.append((phrase, score, is_value))

        values = [item for item in eligible if item[2]][: self.config.dimension_value_size]
        remaining = max(self.config.detection_size - len(values), 0)
        non_values = [item for item in eligible if not item[2]][:remaining]
        return [(phrase, score) for phrase, score, _is_value in (*values, *non_values)]

    def _keyword_threshold(self, entry: SemanticIndexEntry, mode: MapMode) -> float:
        if mode is MapMode.STRICT:
            return 1.0
        is_value = entry.element_type is SemanticElementType.DIMENSION_VALUE
        configured = self.config.value_similarity if is_value else self.config.name_similarity
        minimum = self.config.value_min_similarity if is_value else self.config.name_min_similarity
        mode_step = {MapMode.MODERATE: 2, MapMode.LOOSE: 4, MapMode.ALL: 6}[mode]
        decrease = (configured - minimum) / 4
        return max(configured - mode_step * decrease, minimum)

    def _embedding_matches(
        self,
        *,
        question: str,
        dataset_id: str,
        entries: tuple[SemanticIndexEntry, ...],
        index: SemanticIndexSnapshot,
    ) -> list[SchemaMatch]:
        assert self._embedding_gateway is not None
        # The batch match strategy does not embed the whole question once.
        # It scans short, overlapping spans (word.size=3, word.step=2), recalls the
        # top five entries for every span, then keeps at most ten rounds per span.
        # Short spans are important for Chinese questions where an unknown business
        # synonym can otherwise be diluted by the rest of the sentence.
        segments_with_spans = _embedding_segments_with_spans(question)
        segments = tuple(item[0] for item in segments_with_spans)
        query_batch = self._embedding_gateway.encode(segments)
        if query_batch.model_id != index.embedding_model_id:
            raise ValueError("query embedding model differs from index snapshot")
        if query_batch.dimension != index.vector_dimension or len(query_batch.vectors) != len(
            segments
        ):
            raise ValueError("query embedding dimension differs from index snapshot")
        vector_by_id = {
            entry.id: vector for entry, vector in zip(index.entries, index.vectors, strict=True)
        }
        matches: list[SchemaMatch] = []
        for (segment, detected_spans), query_vector in zip(
            segments_with_spans,
            query_batch.vectors,
            strict=True,
        ):
            if len(normalize_text(segment)) <= 1:
                continue
            ranked: list[tuple[float, SemanticIndexEntry]] = []
            for entry in entries:
                if entry.element_type is SemanticElementType.DATASET:
                    continue
                score = _cosine(query_vector, vector_by_id[entry.id])
                exact_span = _contains(normalize_text(segment), entry.normalized_phrase)
                # 阈值是上游 relevance score 口径((cos+1)/2),不是原始余弦。
                if not exact_span and relevance_score(score) < self.config.embedding_similarity:
                    continue
                # MapFilter.twoCharactersMustEqual: fuzzy matches for one- or
                # two-character dictionary words are too noisy to retain.
                if len(entry.normalized_phrase) <= 2 and score < 1.0 and not exact_span:
                    continue
                ranked.append((score, entry))
            for score, entry in sorted(
                ranked,
                key=lambda item: (-item[0], -item[1].priority, item[1].id),
            )[:5]:
                matches.append(
                    self._match(
                        entry,
                        dataset_id=dataset_id,
                        method=MatchMethod.EMBEDDING,
                        score=score,
                        detected_text=segment,
                        detected_spans=detected_spans,
                    )
                )
        return sorted(
            matches,
            key=lambda item: (-item.score, -item.priority, item.element_type, item.element_id),
        )[: 10 * len(segments)]

    @staticmethod
    def _match(
        entry: SemanticIndexEntry,
        *,
        dataset_id: str,
        method: MatchMethod,
        score: float,
        detected_text: str,
        detected_spans: tuple[tuple[int, int], ...] = (),
    ) -> SchemaMatch:
        return SchemaMatch(
            entry_id=entry.id,
            dataset_id=dataset_id,
            element_type=entry.element_type,
            element_id=entry.element_id,
            phrase=entry.phrase,
            detected_text=detected_text,
            method=method,
            score=max(0.0, min(score, 1.0)),
            priority=entry.priority,
            dimension_id=entry.dimension_id,
            raw_value=entry.raw_value,
            detected_spans=detected_spans,
        )

    def _expand_terms(
        self,
        matches: list[SchemaMatch],
        *,
        entries: tuple[SemanticIndexEntry, ...],
        dataset_id: str,
        mode: MapMode,
        final_stage: bool,
        index: SemanticIndexSnapshot,
    ) -> list[SchemaMatch]:
        """Map an exactly matched term's description through the same mappers.

        Parity source: ``TermDescMapper.java`` remaps ``SchemaElement.description``
        only for fully matched terms and prevents recursive term-description mapping.
        Direct term-to-metric IDs are governance metadata and are deliberately not
        used as an online semantic shortcut.
        """

        by_entry_id = {entry.id: entry for entry in entries}
        expanded: list[SchemaMatch] = []
        for match in matches:
            if match.element_type is not SemanticElementType.TERM or match.score != 1.0:
                continue
            term = by_entry_id.get(match.entry_id)
            if term is None or not term.description.strip():
                continue
            mapped = self._keyword_matches(
                question=term.description,
                normalized_question=normalize_text(term.description),
                dataset_id=dataset_id,
                entries=entries,
                mode=mode,
                # 术语描述的二次映射同样发生在已有匹配之后。
                existing_matches=list(matches),
            )
            # 与主路径同一个开关:上游 TermDescMapper 对术语描述重跑全部 mapper,
            # 而 EmbeddingMapper 在 LLM_OR_RULE 阶段同样 accept。只判 LOOSE 会让
            # 最终 LLM 阶段的术语→指标向量扩展整体丢失。
            if (
                mode is MapMode.LOOSE or final_stage or self._llm_enabled
            ) and self._embedding_gateway is not None:
                mapped.extend(
                    self._embedding_matches(
                        question=term.description,
                        dataset_id=dataset_id,
                        entries=entries,
                        index=index,
                    )
                )
            for mapped_match in mapped:
                if mapped_match.element_type is SemanticElementType.TERM:
                    continue
                expanded.append(
                    mapped_match.model_copy(
                        update={
                            "method": MatchMethod.TERM,
                        }
                    )
                )
        return expanded

    @staticmethod
    def _deduplicate(matches: list[SchemaMatch]) -> list[SchemaMatch]:
        best: dict[tuple[SemanticElementType, str], SchemaMatch] = {}
        for match in matches:
            key = (match.element_type, match.element_id)
            previous = best.get(key)
            if previous is None or (
                match.score,
                _MATCH_METHOD_PRIORITY[match.method],
                match.priority,
                len(match.phrase),
            ) > (
                previous.score,
                _MATCH_METHOD_PRIORITY[previous.method],
                previous.priority,
                len(previous.phrase),
            ):
                best[key] = match
        return sorted(
            best.values(),
            key=lambda item: (-item.score, -item.priority, item.element_type, item.element_id),
        )

    def _ambiguities(self, matches: list[SchemaMatch]) -> tuple[SemanticAmbiguityGroup, ...]:
        by_text: dict[str, list[SchemaMatch]] = defaultdict(list)
        for match in matches:
            if match.element_type not in {
                SemanticElementType.METRIC,
                SemanticElementType.DIMENSION,
                SemanticElementType.DIMENSION_VALUE,
            }:
                continue
            by_text[normalize_text(match.detected_text or match.phrase)].append(match)
        groups: list[SemanticAmbiguityGroup] = []
        for normalized_text, group in sorted(by_text.items()):
            distinct = {(item.element_type, item.element_id): item for item in group}
            if len(distinct) < 2:
                continue
            ordered = tuple(sorted(distinct, key=lambda item: (item[0].value, item[1])))
            detected_text = next(
                (
                    item.detected_text or item.phrase
                    for item in group
                    if normalize_text(item.detected_text or item.phrase) == normalized_text
                ),
                normalized_text,
            )
            groups.append(
                SemanticAmbiguityGroup(
                    detected_text=detected_text,
                    members=tuple(
                        SemanticAmbiguityMember(
                            element_type=element_type,
                            element_id=element_id,
                        )
                        for element_type, element_id in ordered
                    ),
                )
            )
        return tuple(groups)

    @staticmethod
    def _apply_map_filters(matches: list[SchemaMatch]) -> list[SchemaMatch]:
        """Apply ``MapFilter.java`` lexical invariants in upstream order."""

        matches = [item for item in matches if len(normalize_text(item.detected_text)) > 1]
        matches = [
            item for item in matches if len(normalize_text(item.phrase)) > 2 or item.score == 1.0
        ]

        by_detected: dict[str, list[SchemaMatch]] = defaultdict(list)
        for item in matches:
            by_detected[normalize_text(item.detected_text)].append(item)
        exact_per_detected: list[SchemaMatch] = []
        for group in by_detected.values():
            exact = [item for item in group if item.score == 1.0]
            exact_per_detected.extend(exact or group)

        # Upstream MapFilter longest-word behavior: a weak partial hit on a
        # fragment such as "活动" cannot create a second semantic intent when a
        # stronger, longer detected span such as "活动交易额" already covers it.
        # This operates on mapper-authored detected text only; it never reads
        # question keywords to invent or choose a semantic object.
        longest_filtered = [
            item
            for item in exact_per_detected
            if not (
                item.method in _PARTIAL_MATCH_METHODS
                and SemanticMapper._partial_match_is_fully_covered(
                    item,
                    candidates=exact_per_detected,
                )
            )
        ]

        exact_phrases = {
            normalize_text(item.phrase) for item in longest_filtered if item.score == 1.0
        }
        shadowed = {
            short
            for short in exact_phrases
            if any(short != long and short in long for long in exact_phrases)
        }
        return [
            item
            for item in longest_filtered
            if item.score != 1.0 or normalize_text(item.phrase) not in shadowed
        ]

    @staticmethod
    def _partial_match_is_fully_covered(
        item: SchemaMatch,
        *,
        candidates: list[SchemaMatch],
    ) -> bool:
        """Return true only when every occurrence is inside a stronger hit.

        Mapper filtering is span-local.  Comparing only surface strings makes
        ``各活动的活动交易额`` indistinguishable from a question containing
        only the nested ``活动`` in ``活动交易额`` and deletes valid grouping
        evidence.  Legacy hand-built matches without offsets keep the previous
        conservative surface-text behavior.
        """

        return _surface_partial_is_fully_covered(item, candidates)


def _contains(question: str, phrase: str) -> bool:
    if not phrase:
        return False
    if re.fullmatch(r"[a-z0-9_$]+", phrase):
        return re.search(rf"(?<![a-z0-9_$]){re.escape(phrase)}(?![a-z0-9_$])", question) is not None
    return phrase in question


def _surface_partial_is_fully_covered(
    item: SchemaMatch | MappingEvidenceMatch,
    candidates: Sequence[SchemaMatch | MappingEvidenceMatch],
) -> bool:
    short = normalize_text(item.detected_text)
    covering = [
        other
        for other in candidates
        if other is not item
        and other.detected_span_source == item.detected_span_source
        and short
        and short != (long := normalize_text(other.detected_text))
        and short in long
        and len(long) > len(short)
        and other.method in _COVERING_MATCH_METHODS
    ]
    if not covering:
        return False
    if not item.detected_spans:
        return False
    return all(
        any(
            outer_start <= inner_start and inner_end <= outer_end
            for other in covering
            for outer_start, outer_end in other.detected_spans
        )
        for inner_start, inner_end in item.detected_spans
    )


def _filter_global_surface_evidence(
    matches: list[MappingEvidenceMatch],
) -> list[MappingEvidenceMatch]:
    """Apply longest-surface filtering over aggregated surfaces and intervals."""

    normalized = {id(item): normalize_text(item.detected_text) for item in matches}
    span_sequences_by_surface: dict[
        str,
        dict[str, dict[int, tuple[tuple[int, int], ...]]],
    ] = defaultdict(dict)
    partial_surfaces: dict[str, set[str]] = defaultdict(set)
    for item in matches:
        surface = normalized[id(item)]
        if not surface:
            continue
        if item.method in _COVERING_MATCH_METHODS and item.detected_spans:
            sequences = span_sequences_by_surface[item.detected_span_source].setdefault(
                surface,
                {},
            )
            sequence_id = id(item.detected_spans)
            if sequence_id not in sequences:
                sequences[sequence_id] = tuple(sorted(item.detected_spans))
        if item.method in _PARTIAL_MATCH_METHODS:
            partial_surfaces[item.detected_span_source].add(surface)

    covering_sequences: dict[
        tuple[str, str],
        tuple[tuple[tuple[int, int], ...], ...],
    ] = {}
    for source, short_surfaces in partial_surfaces.items():
        surfaces = span_sequences_by_surface[source]
        by_length: dict[
            int,
            list[tuple[str, tuple[tuple[tuple[int, int], ...], ...]]],
        ] = defaultdict(list)
        for surface, sequences in surfaces.items():
            by_length[len(surface)].append((surface, tuple(sequences.values())))
        lengths = sorted(by_length)
        for short in short_surfaces:
            covering_sequences[(source, short)] = tuple(
                spans
                for length in lengths
                if length > len(short)
                for long, long_sequences in by_length[length]
                if short in long
                for spans in long_sequences
            )

    coverage_cache: dict[
        tuple[str, str, int],
        bool,
    ] = {}

    def fully_covered(item: MappingEvidenceMatch) -> bool:
        short = normalized[id(item)]
        if not item.detected_spans:
            return False
        key = (item.detected_span_source, short, id(item.detected_spans))
        cached = coverage_cache.get(key)
        if cached is not None:
            return cached
        sequences = covering_sequences.get((item.detected_span_source, short), ())
        sequence_indexes = [0] * len(sequences)
        maximum_end = -1
        covered = True
        for inner_start, inner_end in sorted(item.detected_spans):
            for sequence_index, sequence in enumerate(sequences):
                outer_index = sequence_indexes[sequence_index]
                while outer_index < len(sequence) and sequence[outer_index][0] <= inner_start:
                    maximum_end = max(maximum_end, sequence[outer_index][1])
                    outer_index += 1
                sequence_indexes[sequence_index] = outer_index
            if maximum_end < inner_end:
                covered = False
                break
        coverage_cache[key] = covered
        return covered

    return [
        item
        for item in matches
        if not (item.method in _PARTIAL_MATCH_METHODS and fully_covered(item))
    ]


@dataclass(frozen=True)
class _SurfaceCoordinates:
    normalized: str
    raw_ranges: tuple[tuple[int, int], ...]
    reliable: bool = True

    def literal_spans(self, phrase: str) -> tuple[tuple[int, int], ...]:
        if not self.reliable:
            return ()
        needle = _normalize_surface(phrase)
        if not needle:
            return ()
        spans = []
        start = self.normalized.find(needle)
        while start >= 0:
            end = start + len(needle)
            matched_ranges = self.raw_ranges[start:end]
            spans.append(
                (
                    min(item[0] for item in matched_ranges),
                    max(item[1] for item in matched_ranges),
                )
            )
            start = self.normalized.find(needle, start + 1)
        return tuple(spans)

    def raw_span(self, start: int, end: int) -> tuple[int, int]:
        if not self.reliable:
            raise ValueError("normalized surface cannot be mapped to raw offsets")
        matched_ranges = self.raw_ranges[start:end]
        return (
            min(item[0] for item in matched_ranges),
            max(item[1] for item in matched_ranges),
        )


def _literal_spans(text: str, phrase: str) -> tuple[tuple[int, int], ...]:
    """Return normalized matches as offsets in the original input string."""

    return _surface_coordinates(text).literal_spans(phrase)


def _normalize_surface(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold())


def _surface_coordinates(text: str) -> _SurfaceCoordinates:
    normalized_chars: list[str] = []
    raw_ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = _normalization_cluster_end(text, start)
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        normalized_cluster = unicodedata.normalize("NFKC", text[start:end]).casefold()
        cluster_ranges = _normalized_cluster_ranges(
            text,
            start=start,
            end=end,
            normalized_cluster=normalized_cluster,
        )
        for character, raw_range in zip(
            normalized_cluster,
            cluster_ranges,
            strict=True,
        ):
            if character.isspace():
                if normalized_chars and normalized_chars[-1] == " ":
                    raw_ranges[-1] = (raw_ranges[-1][0], raw_range[1])
                else:
                    normalized_chars.append(" ")
                    raw_ranges.append(raw_range)
            else:
                normalized_chars.append(character)
                raw_ranges.append(raw_range)
        start = end

    normalized = "".join(normalized_chars)
    expected = _normalize_surface(text)
    if normalized != expected:
        return _SurfaceCoordinates(
            normalized=expected,
            raw_ranges=(),
            reliable=False,
        )
    return _SurfaceCoordinates(
        normalized=normalized,
        raw_ranges=tuple(raw_ranges),
    )


def _normalization_cluster_end(text: str, start: int) -> int:
    end = start + 1
    if (
        _is_hangul_lv_syllable(text[start])
        and end < len(text)
        and _hangul_jamo_role(text[end]) == "T"
    ):
        return end + 1
    if _hangul_jamo_role(text[start]) != "L" or end >= len(text):
        return end
    if _hangul_jamo_role(text[end]) != "V":
        return end
    end += 1
    if end < len(text) and _hangul_jamo_role(text[end]) == "T":
        end += 1
    return end


def _normalized_cluster_ranges(
    text: str,
    *,
    start: int,
    end: int,
    normalized_cluster: str,
) -> tuple[tuple[int, int], ...]:
    ranges_by_character: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for raw_index in range(start, end):
        normalized_character = unicodedata.normalize("NFKC", text[raw_index]).casefold()
        for character in normalized_character:
            ranges_by_character[character].append((raw_index, raw_index + 1))
    if sum(len(items) for items in ranges_by_character.values()) != len(normalized_cluster):
        return tuple((start, end) for _character in normalized_cluster)
    offsets: dict[str, int] = defaultdict(int)
    attributed = []
    for character in normalized_cluster:
        index = offsets[character]
        candidates = ranges_by_character.get(character, ())
        if index >= len(candidates):
            return tuple((start, end) for _character in normalized_cluster)
        attributed.append(candidates[index])
        offsets[character] += 1
    return tuple(attributed)


def _is_hangul_lv_syllable(character: str) -> bool:
    codepoint = ord(character)
    return 0xAC00 <= codepoint <= 0xD7A3 and (codepoint - 0xAC00) % 28 == 0


def _hangul_jamo_role(character: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", character)
    if len(normalized) != 1:
        return None
    codepoint = ord(normalized)
    if 0x1100 <= codepoint <= 0x115F or 0xA960 <= codepoint <= 0xA97C:
        return "L"
    if 0x1160 <= codepoint <= 0x11A7 or 0xD7B0 <= codepoint <= 0xD7C6:
        return "V"
    if 0x11A8 <= codepoint <= 0x11FF or 0xD7CB <= codepoint <= 0xD7FB:
        return "T"
    return None


def _dictionary_segments(
    question: str,
    entries: tuple[SemanticIndexEntry, ...],
) -> tuple[str, ...]:
    """Port HanLP custom terms plus ``SingleMatchStrategy`` offset stepping."""

    return tuple(_dictionary_segment_spans(question, entries))


def _dictionary_segment_spans(
    question: str,
    entries: tuple[SemanticIndexEntry, ...],
) -> dict[str, tuple[tuple[int, int], ...]]:
    """Return only scan segments that can produce governed lexical evidence.

    The original SingleMatchStrategy enumerates every start/end pair.  Materializing
    those substrings is quadratic in question length even though a segment longer
    than every indexed phrase, or absent from every indexed phrase, cannot satisfy
    any downstream prefix/suffix/name condition.  Pruning those impossible spans is
    semantics-preserving and bounds work by the governed dictionary vocabulary.
    """

    coordinates = _surface_coordinates(question)
    normalized = coordinates.normalized
    phrases = tuple(
        dict.fromkeys(entry.normalized_phrase for entry in entries if entry.normalized_phrase)
    )
    if not normalized or not phrases:
        return {}
    maximum_phrase_length = max(len(phrase) for phrase in phrases)
    terms = HanlpCustomDictionary(entries).segment(normalized)
    registered_offsets = {term.offset: term.length for term in terms}

    segments: dict[str, list[tuple[int, int]]] = {}
    start = 0
    while start < len(normalized):
        end = start
        while end <= len(normalized):
            end += registered_offsets.get(end, 1)
            if end <= len(normalized):
                scanned = normalized[start:end]
                trimmed_start = start + (len(scanned) - len(scanned.lstrip()))
                trimmed_end = end - (len(scanned) - len(scanned.rstrip()))
                segment = normalize_text(normalized[trimmed_start:trimmed_end])
                if len(segment) > maximum_phrase_length:
                    break
                if segment and any(segment in phrase for phrase in phrases):
                    segments.setdefault(segment, []).append((trimmed_start, trimmed_end))
        start += registered_offsets.get(start, 1)
    if not coordinates.reliable:
        return {segment: () for segment in segments}
    return {
        segment: tuple(dict.fromkeys(coordinates.raw_span(start, end) for start, end in spans))
        for segment, spans in segments.items()
    }


def _edit_similarity(left: str, right: str) -> float:
    """Edit similarity that also forgives one adjacent transposition."""

    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    rows = len(left) + 1
    columns = len(right) + 1
    distance = [[0] * columns for _ in range(rows)]
    for row in range(rows):
        distance[row][0] = row
    for column in range(columns):
        distance[0][column] = column
    for row in range(1, rows):
        for column in range(1, columns):
            if left[row - 1] == right[column - 1]:
                distance[row][column] = distance[row - 1][column - 1]
            elif (
                row > 1
                and column > 1
                and left[row - 1] == right[column - 2]
                and right[column - 1] == left[row - 2]
            ):
                distance[row][column] = 1 + min(
                    distance[row - 2][column - 2],
                    distance[row][column - 1],
                    distance[row - 1][column],
                )
            else:
                distance[row][column] = min(
                    distance[row - 1][column - 1] + 1,
                    distance[row][column - 1] + 1,
                    distance[row - 1][column] + 1,
                )
    return 1 - distance[-1][-1] / max(len(left), len(right))


def _embedding_segments(
    question: str,
    entries: tuple[SemanticIndexEntry, ...] = (),
    *,
    size: int = 3,
    step: int = 2,
) -> tuple[str, ...]:
    """Union governed dictionary words with the upstream sliding window.

    The window (upstream ``BatchMatchStrategy``: size 3, step 2) starts only at
    even offsets, so whether a real word is ever embedded depends on its parity
    in the question: 「图书馆」 is queried in 「各城市的图书馆数量」 but never in
    「各图书馆的藏品数量」.  Measured with the production model,
    cos(图书馆, 图书馆名称)=0.8318 clears the 0.90 relevance bar while every
    window fragment (各图书 0.798, 书馆的 0.825) fails it, so the recall was
    decided by offset parity rather than by meaning.

    Governed words come first because they are real terms; the window is kept
    so wording outside the governed vocabulary still has undirected coverage.
    """

    return tuple(
        segment
        for segment, _spans in _embedding_segments_with_spans(
            question,
            entries,
            size=size,
            step=step,
        )
    )


def _embedding_segments_with_spans(
    question: str,
    entries: tuple[SemanticIndexEntry, ...] = (),
    *,
    size: int = 3,
    step: int = 2,
) -> tuple[tuple[str, tuple[tuple[int, int], ...]], ...]:
    normalized = " ".join(question.split())
    windows = tuple(
        dict.fromkeys(
            normalized[start : min(start + size, len(normalized))].strip()
            for start in range(0, len(normalized), step)
            if normalized[start : min(start + size, len(normalized))].strip()
        )
    )
    governed = _dictionary_segment_spans(question, entries) if entries else {}
    segments = tuple(dict.fromkeys((*governed, *windows))) or (normalized,)
    coordinates = _surface_coordinates(question)
    return tuple(
        (
            segment,
            tuple(
                dict.fromkeys(
                    (
                        *governed.get(normalize_text(segment), ()),
                        *coordinates.literal_spans(segment),
                    )
                )
            ),
        )
        for segment in segments
    )


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(sum(item * item for item in left)) * math.sqrt(
        sum(item * item for item in right)
    )
    if denominator == 0:
        return 0.0
    return numerator / denominator
