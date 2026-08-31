from __future__ import annotations

import pytest

import knowflow_analytics.query.mapper as mapper_module
from knowflow_analytics.query.contracts import (
    MapMode,
    MappingEvidenceChannel,
    MappingEvidenceMatch,
    MatchMethod,
    SchemaMatch,
)
from knowflow_analytics.query.mapper import (
    SemanticMapper,
    _dictionary_segment_spans,
    _embedding_segments_with_spans,
    _filter_global_surface_evidence,
    _literal_spans,
)
from knowflow_analytics.semantic.index import (
    EmbeddingBatch,
    IndexState,
    SemanticElementType,
    SemanticIndexBuilder,
    SemanticIndexEntry,
    SemanticIndexSnapshot,
)


class _ConstantEmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="mapper-test",
            dimension=1,
            vectors=tuple((1.0,) for _ in texts),
        )


class _OrderAmountEmbeddingGateway:
    """Make only the real order-amount phrase recall the net-amount metric."""

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        vectors = []
        for text in texts:
            if text == "订单额":
                vectors.append((0.8, 0.0, 0.6))
            elif text == "订单净金额":
                vectors.append((1.0, 0.0, 0.0))
            elif text == "订单退款":
                vectors.append((0.0, 1.0, 0.0))
            else:
                vectors.append((0.0, 0.0, 1.0))
        return EmbeddingBatch(
            model_id="order-amount-test",
            dimension=3,
            vectors=tuple(vectors),
        )


def test_dictionary_scan_retains_only_segments_that_can_match_the_index() -> None:
    entry = SemanticIndexEntry(
        id="entry-net-revenue",
        phrase="净收入",
        normalized_phrase="净收入",
        element_type=SemanticElementType.METRIC,
        element_id="net-revenue",
        dataset_ids=("sales",),
        source="name",
        priority=300,
    )
    question = f"{'甲' * 300}净收入{'乙' * 300}"

    segments = _dictionary_segment_spans(question, (entry,))

    assert "净收入" in segments
    assert all(segment in entry.normalized_phrase for segment in segments)
    assert sum(len(spans) for spans in segments.values()) <= (
        len(question) * len(entry.normalized_phrase)
    )


def test_dictionary_scan_normalizes_newlines_before_segment_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = SemanticIndexEntry(
        id="entry-net-revenue",
        phrase="净收入",
        normalized_phrase="净收入",
        element_type=SemanticElementType.METRIC,
        element_id="net-revenue",
        dataset_ids=("sales",),
        source="name",
        priority=300,
    )
    calls = 0
    original = mapper_module.normalize_text

    def counting_normalize(value: str) -> str:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(mapper_module, "normalize_text", counting_normalize)

    assert _dictionary_segment_spans("\n" * 300, (entry,)) == {}
    assert calls <= 2


def test_embedding_spans_share_dictionary_nfkc_casefold_coordinates() -> None:
    entry = SemanticIndexEntry(
        id="entry-refund",
        phrase="订单退款",
        normalized_phrase="订单退款",
        element_type=SemanticElementType.METRIC,
        element_id="refund_amount",
        dataset_ids=("orders",),
        source="name",
        priority=300,
    )

    assert _literal_spans("ßßß订单x订单额", "订单额") == ((6, 9),)
    assert _dictionary_segment_spans("ßßß订单x订单额", (entry,))["订单"] == (
        (3, 5),
        (6, 8),
    )
    assert _literal_spans("ﬃ订单额", "订单额") == ((1, 4),)
    assert _literal_spans("가订单额", "가") == ((0, 2),)
    reordered = "ﬀ각Σͅ中ßᆨA①́ͅς\tA "
    assert _literal_spans(reordered, "ισ a") == ((10, 15),)
    assert _literal_spans("①́ͅ", "́ι") == ((1, 3),)
    assert _dictionary_segment_spans("ﬃ订单额", (entry,))["订单"] == ((1, 3),)


def test_governed_embedding_segment_reuses_dictionary_internal_whitespace_spans() -> None:
    entry = SemanticIndexEntry(
        id="entry-order-amount",
        phrase="order amount",
        normalized_phrase="orderamount",
        element_type=SemanticElementType.METRIC,
        element_id="order_amount",
        dataset_ids=("orders",),
        source="name",
        priority=300,
    )

    segments = dict(_embedding_segments_with_spans("order   amount", (entry,)))

    assert segments["orderamount"] == ((0, 14),)


def test_evidence_candidates_share_one_immutable_surface_span_sequence() -> None:
    spans = tuple((offset, offset + 2) for offset in range(0, 2_000, 2))
    entries = tuple(
        SemanticIndexEntry(
            id=f"entry-{index}",
            phrase=f"metric-{index}",
            normalized_phrase=f"metric-{index}",
            element_type=SemanticElementType.METRIC,
            element_id=f"metric-{index}",
            dataset_ids=("sales",),
            source="name",
            priority=300,
        )
        for index in range(2)
    )

    evidence = tuple(
        SemanticMapper._evidence_match(
            entry,
            eligible_dataset_ids=("sales",),
            channel=MappingEvidenceChannel.EMBEDDING,
            method=MatchMethod.EMBEDDING,
            score=0.95,
            detected_text="shared surface",
            detected_spans=spans,
        )
        for entry in entries
    )
    projected = SemanticMapper._schema_match(evidence[0], dataset_id="sales")

    assert evidence[0].detected_spans is spans
    assert evidence[1].detected_spans is spans
    assert projected.detected_spans is spans


def test_global_surface_filter_is_name_type_and_scope_agnostic() -> None:
    common = {
        "score": 1.0,
        "priority": 300,
        "channel": MappingEvidenceChannel.DICTIONARY,
        "entry_source": "name",
    }
    short = MappingEvidenceMatch(
        entry_id="entry-short",
        eligible_dataset_ids=("scope-b",),
        element_type=SemanticElementType.METRIC,
        element_id="short",
        phrase="alpha",
        normalized_phrase="alpha",
        detected_text="a",
        method=MatchMethod.KEYWORD,
        detected_spans=((0, 1), (4, 5)),
        **common,
    )
    long_width_variant = MappingEvidenceMatch(
        entry_id="entry-long-width",
        eligible_dataset_ids=("scope-a",),
        element_type=SemanticElementType.DIMENSION,
        element_id="long-width",
        phrase="alpha beta",
        normalized_phrase="alphabeta",
        detected_text="ＡＢ",
        method=MatchMethod.EXACT,
        detected_spans=((0, 2),),
        **common,
    )
    long_ascii_variant = MappingEvidenceMatch(
        entry_id="entry-long-ascii",
        eligible_dataset_ids=("scope-a",),
        element_type=SemanticElementType.DIMENSION,
        element_id="long-ascii",
        phrase="alpha beta",
        normalized_phrase="alphabeta",
        detected_text="ab",
        method=MatchMethod.EXACT,
        detected_spans=((4, 6),),
        **common,
    )

    filtered = _filter_global_surface_evidence([short, long_width_variant, long_ascii_variant])

    assert short not in filtered
    assert filtered == [long_width_variant, long_ascii_variant]


def test_strict_mapping_finds_metric_dimension_and_value(sales_index):
    result = SemanticMapper().map(
        question="华东各区域净收入 Top 10",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )

    matched = {(item.element_type, item.element_id) for item in result.matches}
    assert (SemanticElementType.METRIC, "net_revenue") in matched
    assert (SemanticElementType.DIMENSION, "region") in matched
    assert (SemanticElementType.DIMENSION_VALUE, "region_east") in matched
    assert all(item.method is MatchMethod.EXACT for item in result.matches)


def test_term_mapping_expands_to_governed_metric(sales_index):
    result = SemanticMapper().map(
        question="按渠道看销售额",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )

    metric = next(item for item in result.matches if item.element_id == "net_revenue")
    assert metric.method is MatchMethod.TERM
    assert metric.score == 1.0


def test_loose_mapping_records_exact_only_degradation_without_embedding(sales_index):
    result = SemanticMapper().map(
        question="完全无法匹配的词",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.LOOSE,
    )

    assert result.degraded_reasons == ("embedding_gateway_unavailable",)


def test_all_mode_never_expands_beyond_selected_dataset(sales_index):
    result = SemanticMapper().map(
        question="未知分析",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.ALL,
    )

    assert result.matches
    assert all(item.dataset_id == "sales_dataset" for item in result.matches)
    assert all(item.method is MatchMethod.ALL_FIELD for item in result.matches)


def test_all_mode_appends_fields_without_dropping_exact_dimension_values(sales_release):
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(sales_release)

    result = SemanticMapper(embedding_gateway=_ConstantEmbeddingGateway()).map(
        question="华东净收入",
        dataset_id="sales_dataset",
        index=index,
        mode=MapMode.ALL,
        final_stage=True,
    )

    exact_value = next(item for item in result.matches if item.element_id == "region_east")
    assert exact_value.method is MatchMethod.EXACT
    assert any(item.method is MatchMethod.ALL_FIELD for item in result.matches)


def test_final_llm_stage_runs_embedding_even_after_keyword_discovery(sales_release):
    gateway = _ConstantEmbeddingGateway()
    index = SemanticIndexBuilder(gateway).build(sales_release)

    result = SemanticMapper(embedding_gateway=gateway).map(
        question="业务表现",
        dataset_id="sales_dataset",
        index=index,
        mode=MapMode.STRICT,
        final_stage=True,
    )

    assert any(item.method is MatchMethod.EMBEDDING for item in result.matches)


def test_exact_span_resolution_prefers_more_specific_nested_semantic_phrase():
    index = _nested_phrase_index()

    result = SemanticMapper().map(
        question="各平台参加活动商家数量",
        dataset_id="business",
        index=index,
        mode=MapMode.STRICT,
    )

    element_ids = {item.element_id for item in result.matches}
    assert "activity_merchant_count" in element_ids
    assert "merchant_count" not in element_ids


@pytest.mark.parametrize(
    ("question", "expected_span"),
    (
        ("直营渠道订单额占比多少", (4, 7)),
        ("订单额在直营渠道中的占比", (0, 3)),
        ("a   订单额", (4, 7)),
    ),
)
def test_embedding_long_phrase_suppresses_a_keyword_hit_on_its_nested_substring(
    question: str,
    expected_span: tuple[int, int],
):
    gateway = _OrderAmountEmbeddingGateway()
    index = _order_amount_index(gateway)

    result = SemanticMapper(embedding_gateway=gateway, llm_enabled=True).map(
        question=question,
        dataset_id="orders",
        index=index,
        mode=MapMode.MODERATE,
    )

    metrics = {
        item.element_id: item
        for item in result.matches
        if item.element_type is SemanticElementType.METRIC
    }
    assert set(metrics) == {"net_amount"}
    assert metrics["net_amount"].detected_text == "订单额"
    assert metrics["net_amount"].detected_spans == (expected_span,)
    assert not any(
        item.element_id == "order_date" and item.detected_text == "订单" for item in result.matches
    )


def test_independent_refund_and_order_amount_mentions_remain_two_metrics():
    gateway = _OrderAmountEmbeddingGateway()
    index = _order_amount_index(gateway)

    result = SemanticMapper(embedding_gateway=gateway, llm_enabled=True).map(
        question="订单退款以及订单额分别多少",
        dataset_id="orders",
        index=index,
        mode=MapMode.MODERATE,
    )

    metrics = {
        item.element_id: item
        for item in result.matches
        if item.element_type is SemanticElementType.METRIC
    }
    assert set(metrics) == {"refund_amount", "net_amount"}
    assert metrics["refund_amount"].method is MatchMethod.EXACT
    assert metrics["net_amount"].method is MatchMethod.EMBEDDING
    assert metrics["net_amount"].detected_spans == ((6, 9),)


def test_embedding_cover_does_not_remove_an_independent_short_occurrence():
    short = SchemaMatch(
        entry_id="entry-refund",
        dataset_id="orders",
        element_type=SemanticElementType.METRIC,
        element_id="refund_amount",
        phrase="订单退款",
        detected_text="订单",
        method=MatchMethod.KEYWORD,
        score=0.5,
        priority=300,
        detected_spans=((0, 2), (3, 5)),
    )
    long = SchemaMatch(
        entry_id="entry-net",
        dataset_id="orders",
        element_type=SemanticElementType.METRIC,
        element_id="net_amount",
        phrase="订单净金额",
        detected_text="订单额",
        method=MatchMethod.EMBEDDING,
        score=0.9,
        priority=300,
        detected_spans=((3, 6),),
    )

    filtered = SemanticMapper._apply_map_filters([short, long])

    assert {item.element_id for item in filtered} == {"refund_amount", "net_amount"}


def test_legacy_embedding_projection_carries_the_same_surface_spans():
    gateway = _OrderAmountEmbeddingGateway()
    index = _order_amount_index(gateway)

    matches = SemanticMapper(embedding_gateway=gateway)._embedding_matches(
        question="直营渠道订单额占比多少",
        dataset_id="orders",
        entries=index.entries,
        index=index,
    )

    net_amount = next(item for item in matches if item.element_id == "net_amount")
    assert net_amount.detected_text == "订单额"
    assert net_amount.detected_spans == ((4, 7),)


def test_exact_match_filter_removes_shorter_full_match_globally():
    index = _nested_phrase_index()

    result = SemanticMapper().map(
        question="各活动的活动交易额",
        dataset_id="business",
        index=index,
        mode=MapMode.STRICT,
    )

    element_ids = {item.element_id for item in result.matches}
    assert "activity_revenue" in element_ids
    assert "activity" not in element_ids


def test_exact_span_resolution_removes_covered_technical_name_from_another_element():
    index = _nested_phrase_index()

    result = SemanticMapper().map(
        question="各客户所属区域的商家数",
        dataset_id="business",
        index=index,
        mode=MapMode.STRICT,
    )

    element_ids = {item.element_id for item in result.matches}
    assert "customer_region" in element_ids
    assert "other_region" not in element_ids


def test_exact_span_resolution_does_not_hide_same_surface_ambiguity():
    index = _nested_phrase_index(include_ambiguous_activity=True)

    result = SemanticMapper().map(
        question="活动",
        dataset_id="business",
        index=index,
        mode=MapMode.STRICT,
    )

    element_ids = {item.element_id for item in result.matches}
    assert {"activity", "campaign"} <= element_ids
    assert any(set(group) == {"activity", "campaign"} for group in result.ambiguous_groups)


def test_keyword_mapper_removes_one_character_matches_even_with_dimension_context():
    index = _boolean_value_index()

    explicit = SemanticMapper().map(
        question="是否985为是",
        dataset_id="education",
        index=index,
        mode=MapMode.STRICT,
    )

    assert {item.element_id for item in explicit.matches} == {"is_985"}
    assert explicit.ambiguous_groups == ()


def test_moderate_mapping_uses_segment_edit_distance_not_whole_question_ratio(sales_index):
    result = SemanticMapper().map(
        question="查询净收",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.MODERATE,
    )

    metric = next(item for item in result.matches if item.element_id == "net_revenue")
    assert metric.method is MatchMethod.KEYWORD
    assert metric.detected_text == "净收"
    assert metric.score == pytest.approx(2 / 3)


def test_database_match_recalls_a_middle_substring_of_the_primary_name() -> None:
    index = _database_match_index()

    result = SemanticMapper().map(
        question="订单数",
        dataset_id="dataset",
        index=index,
        mode=MapMode.MODERATE,
    )

    metric = next(item for item in result.matches if item.element_id == "effective_order_count")
    assert metric.method is MatchMethod.KEYWORD
    assert metric.phrase == "有效订单数量"
    assert metric.detected_text == "订单数"
    assert metric.score == pytest.approx(0.5)


def test_database_match_does_not_treat_an_alias_as_a_primary_schema_name() -> None:
    index = _database_match_index()

    result = SemanticMapper().map(
        question="渠道营收",
        dataset_id="dataset",
        index=index,
        mode=MapMode.MODERATE,
    )

    assert "revenue" not in {item.element_id for item in result.matches}


def test_database_match_keeps_normal_threshold_after_a_dimension_value_match() -> None:
    entries = (
        SemanticIndexEntry(
            id="entry-value",
            phrase="甲乙",
            normalized_phrase="甲乙",
            element_type=SemanticElementType.DIMENSION_VALUE,
            element_id="value-ab",
            dataset_ids=("dataset",),
            source="value",
            priority=300,
            dimension_id="dimension",
            raw_value="甲乙",
        ),
        SemanticIndexEntry(
            id="entry-long-metric",
            phrase="甲乙丙丁戊己庚辛",
            normalized_phrase="甲乙丙丁戊己庚辛",
            element_type=SemanticElementType.METRIC,
            element_id="long-metric",
            dataset_ids=("dataset",),
            source="name",
            priority=300,
        ),
    )
    index = SemanticIndexSnapshot(
        id="idx-value-threshold",
        release_spec_hash="spec-value-threshold",
        content_hash="hash-value-threshold",
        state=IndexState.READY,
        embedding_model_id="test",
        vector_dimension=1,
        entries=entries,
        vectors=((1.0,), (1.0,)),
    )

    mapper = SemanticMapper()
    value_match = mapper._match(
        entries[0],
        dataset_id="dataset",
        method=MatchMethod.EXACT,
        score=1.0,
        detected_text="甲乙",
    )
    result = mapper._database_name_matches(
        segments=("甲乙",),
        entries=index.entries,
        dataset_id="dataset",
        mode=MapMode.MODERATE,
        # 形参更名:上游判定读的是整个 mapInfo,不止字典命中。
        existing_matches=[value_match],
    )

    assert result == []


def test_registered_exact_word_blocks_inner_substring_fuzzy_recall():
    """Parity: SingleMatchStrategy skips offsets inside a registered HanLP word."""

    entries = (
        SemanticIndexEntry(
            id="entry-exact",
            phrase="甲乙丙",
            normalized_phrase="甲乙丙",
            element_type=SemanticElementType.METRIC,
            element_id="metric_exact",
            dataset_ids=("dataset",),
            source="name",
            priority=300,
        ),
        SemanticIndexEntry(
            id="entry-inner-fuzzy",
            phrase="前乙丙后",
            normalized_phrase="前乙丙后",
            element_type=SemanticElementType.METRIC,
            element_id="metric_inner_fuzzy",
            dataset_ids=("dataset",),
            source="name",
            priority=300,
        ),
    )
    index = SemanticIndexSnapshot(
        id="idx-registered-offset",
        release_spec_hash="spec-registered-offset",
        content_hash="hash-registered-offset",
        state=IndexState.READY,
        embedding_model_id="test",
        vector_dimension=1,
        entries=entries,
        vectors=((1.0,), (1.0,)),
    )

    result = SemanticMapper().map(
        question="甲乙丙",
        dataset_id="dataset",
        index=index,
        mode=MapMode.MODERATE,
    )

    assert {item.element_id for item in result.matches} == {"metric_exact"}


def test_database_match_adds_names_after_the_hanlp_eight_candidate_cap():
    """Parity: KeywordMapper runs DatabaseMatch after capped HanLP matching."""

    index = _candidate_limit_index(metric_count=12)

    result = SemanticMapper().map(
        question="ab",
        dataset_id="dataset",
        index=index,
        mode=MapMode.MODERATE,
    )

    assert len(result.matches) == 12
    assert all(item.element_type is SemanticElementType.METRIC for item in result.matches)


def test_database_match_adds_metrics_but_not_values_after_the_hanlp_cap():
    """Parity: DatabaseMatch only supplements unregistered metrics/dimensions."""

    index = _candidate_limit_index(metric_count=10, value_count=3)

    result = SemanticMapper().map(
        question="ab",
        dataset_id="dataset",
        index=index,
        mode=MapMode.MODERATE,
    )

    values = [
        item for item in result.matches if item.element_type is SemanticElementType.DIMENSION_VALUE
    ]
    metrics = [item for item in result.matches if item.element_type is SemanticElementType.METRIC]
    assert len(values) == 1
    assert len(metrics) == 10


def test_keyword_round_selection_is_invariant_to_index_entry_order():
    """Candidate truncation must depend on governed text, not storage order."""

    index = _candidate_limit_index(metric_count=12, value_count=3)
    reversed_index = index.model_copy(
        update={
            "id": "idx-candidate-limit-reversed",
            "entries": tuple(reversed(index.entries)),
            "vectors": tuple(reversed(index.vectors)),
        }
    )

    original = SemanticMapper().map(
        question="ab",
        dataset_id="dataset",
        index=index,
        mode=MapMode.MODERATE,
    )
    reversed_result = SemanticMapper().map(
        question="ab",
        dataset_id="dataset",
        index=reversed_index,
        mode=MapMode.MODERATE,
    )

    assert {
        (item.element_type, item.element_id, item.detected_text, item.score)
        for item in original.matches
    } == {
        (item.element_type, item.element_id, item.detected_text, item.score)
        for item in reversed_result.matches
    }


def test_mapper_behavior_is_invariant_to_semantic_id_renaming(sales_index):
    renamed_index = sales_index.model_copy(
        update={
            "id": "renamed-index",
            "content_hash": "renamed-hash",
            "entries": tuple(
                entry.model_copy(
                    update={
                        "id": f"renamed:{entry.id}",
                        "element_id": f"renamed:{entry.element_id}",
                        "dimension_id": (
                            f"renamed:{entry.dimension_id}"
                            if entry.dimension_id is not None
                            else None
                        ),
                    }
                )
                for entry in sales_index.entries
            ),
        }
    )

    original = SemanticMapper().map(
        question="查询净收",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.MODERATE,
    )
    renamed = SemanticMapper().map(
        question="查询净收",
        dataset_id="sales_dataset",
        index=renamed_index,
        mode=MapMode.MODERATE,
    )

    original_metric = next(item for item in original.matches if item.element_id == "net_revenue")
    renamed_metric = next(
        item for item in renamed.matches if item.element_id == "renamed:net_revenue"
    )
    assert (
        renamed_metric.phrase,
        renamed_metric.method,
        renamed_metric.detected_text,
        renamed_metric.score,
    ) == (
        original_metric.phrase,
        original_metric.method,
        original_metric.detected_text,
        original_metric.score,
    )


def test_term_description_is_mapped_by_the_same_schema_mapper(sales_release):
    term = sales_release.terms[0].model_copy(update={"description": "净收入", "metric_ids": ()})
    release = sales_release.model_copy(update={"terms": (term,)})
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)

    result = SemanticMapper().map(
        question="按渠道看销售额",
        dataset_id="sales_dataset",
        index=index,
        mode=MapMode.STRICT,
    )

    metric = next(item for item in result.matches if item.element_id == "net_revenue")
    assert metric.method is MatchMethod.TERM


def test_selected_moderate_only_term_keeps_term_provenance_in_strict_view(
    sales_release,
) -> None:
    term = sales_release.terms[0].model_copy(update={"description": "净收"})
    release = sales_release.model_copy(update={"terms": (term,)})
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    mapper = SemanticMapper()
    evidence = mapper.collect_evidence(
        question="销售额",
        dataset_ids=("sales_dataset",),
        index=index,
    )
    moderate = mapper.project_evidence(
        evidence=evidence,
        dataset_id="sales_dataset",
        mode=MapMode.MODERATE,
    )
    moderate_match = next(item for item in moderate.matches if item.element_id == "net_revenue")
    assert moderate_match.method is MatchMethod.TERM

    selected = mapper.project_evidence(
        evidence=evidence,
        dataset_id="sales_dataset",
        mode=MapMode.STRICT,
        selected_element_id="net_revenue",
        selected_element_type=SemanticElementType.METRIC,
    )
    selected_match = next(item for item in selected.matches if item.element_id == "net_revenue")

    assert selected_match.method is MatchMethod.TERM
    assert selected_match.score == moderate_match.score
    assert selected_match.detected_span_source == moderate_match.detected_span_source


def _nested_phrase_index(*, include_ambiguous_activity: bool = False) -> SemanticIndexSnapshot:
    definitions = [
        ("activity_merchant_count", "参加活动商家数量", SemanticElementType.METRIC),
        ("merchant_count", "商家数", SemanticElementType.METRIC),
        ("activity_revenue", "活动交易额", SemanticElementType.METRIC),
        ("activity", "活动", SemanticElementType.DIMENSION),
        ("platform", "平台", SemanticElementType.DIMENSION),
        ("customer_region", "客户所属区域", SemanticElementType.DIMENSION),
        ("other_region", "区域", SemanticElementType.DIMENSION),
    ]
    if include_ambiguous_activity:
        definitions.append(("campaign", "活动", SemanticElementType.DIMENSION))
    entries = tuple(
        SemanticIndexEntry(
            id=f"entry-{element_id}",
            phrase=phrase,
            normalized_phrase=phrase,
            element_type=element_type,
            element_id=element_id,
            dataset_ids=("business",),
            source="name",
            priority=300,
        )
        for element_id, phrase, element_type in definitions
    )
    return SemanticIndexSnapshot(
        id="idx-nested",
        release_spec_hash="spec-nested",
        content_hash="hash-nested",
        state=IndexState.READY,
        embedding_model_id="test",
        vector_dimension=1,
        entries=entries,
        vectors=tuple((1.0,) for _item in entries),
    )


def _order_amount_index(
    gateway: _OrderAmountEmbeddingGateway,
) -> SemanticIndexSnapshot:
    entries = (
        SemanticIndexEntry(
            id="entry-net-amount",
            phrase="订单净金额",
            normalized_phrase="订单净金额",
            element_type=SemanticElementType.METRIC,
            element_id="net_amount",
            dataset_ids=("orders",),
            source="name",
            priority=300,
        ),
        SemanticIndexEntry(
            id="entry-refund-amount",
            phrase="订单退款",
            normalized_phrase="订单退款",
            element_type=SemanticElementType.METRIC,
            element_id="refund_amount",
            dataset_ids=("orders",),
            source="name",
            priority=300,
        ),
        SemanticIndexEntry(
            id="entry-order-date",
            phrase="订单日期",
            normalized_phrase="订单日期",
            element_type=SemanticElementType.DIMENSION,
            element_id="order_date",
            dataset_ids=("orders",),
            source="name",
            priority=300,
        ),
    )
    vectors = gateway.encode(tuple(item.phrase for item in entries)).vectors
    return SemanticIndexSnapshot(
        id="idx-order-amount",
        release_spec_hash="spec-order-amount",
        content_hash="hash-order-amount",
        state=IndexState.READY,
        embedding_model_id="order-amount-test",
        vector_dimension=3,
        entries=entries,
        vectors=vectors,
    )


def _boolean_value_index() -> SemanticIndexSnapshot:
    entries = (
        SemanticIndexEntry(
            id="entry-is-985",
            phrase="是否985",
            normalized_phrase="是否985",
            element_type=SemanticElementType.DIMENSION,
            element_id="is_985",
            dataset_ids=("education",),
            source="name",
            priority=300,
        ),
        SemanticIndexEntry(
            id="entry-is-211",
            phrase="是否211",
            normalized_phrase="是否211",
            element_type=SemanticElementType.DIMENSION,
            element_id="is_211",
            dataset_ids=("education",),
            source="name",
            priority=300,
        ),
        SemanticIndexEntry(
            id="entry-is-985-yes",
            phrase="是",
            normalized_phrase="是",
            element_type=SemanticElementType.DIMENSION_VALUE,
            element_id="is_985_yes",
            dataset_ids=("education",),
            source="value",
            priority=300,
            dimension_id="is_985",
            raw_value="是",
        ),
        SemanticIndexEntry(
            id="entry-is-211-yes",
            phrase="是",
            normalized_phrase="是",
            element_type=SemanticElementType.DIMENSION_VALUE,
            element_id="is_211_yes",
            dataset_ids=("education",),
            source="value",
            priority=300,
            dimension_id="is_211",
            raw_value="是",
        ),
    )
    return SemanticIndexSnapshot(
        id="idx-boolean",
        release_spec_hash="spec-boolean",
        content_hash="hash-boolean",
        state=IndexState.READY,
        embedding_model_id="test",
        vector_dimension=1,
        entries=entries,
        vectors=tuple((1.0,) for _item in entries),
    )


def _candidate_limit_index(
    *,
    metric_count: int,
    value_count: int = 0,
) -> SemanticIndexSnapshot:
    entries = tuple(
        [
            SemanticIndexEntry(
                id=f"entry-metric-{index:02d}",
                phrase=f"abm{index:02d}",
                normalized_phrase=f"abm{index:02d}",
                element_type=SemanticElementType.METRIC,
                element_id=f"metric-{index:02d}",
                dataset_ids=("dataset",),
                source="name",
                priority=300,
            )
            for index in range(metric_count)
        ]
        + [
            SemanticIndexEntry(
                id=f"entry-value-{index:02d}",
                phrase=f"abv{index:02d}",
                normalized_phrase=f"abv{index:02d}",
                element_type=SemanticElementType.DIMENSION_VALUE,
                element_id=f"value-{index:02d}",
                dataset_ids=("dataset",),
                source="value",
                priority=300,
                dimension_id="dimension",
                raw_value=f"value-{index:02d}",
            )
            for index in range(value_count)
        ]
    )
    return SemanticIndexSnapshot(
        id="idx-candidate-limit",
        release_spec_hash="spec-candidate-limit",
        content_hash="hash-candidate-limit",
        state=IndexState.READY,
        embedding_model_id="test",
        vector_dimension=1,
        entries=entries,
        vectors=tuple((1.0,) for _item in entries),
    )


def _database_match_index() -> SemanticIndexSnapshot:
    entries = (
        SemanticIndexEntry(
            id="entry-effective-order-count",
            phrase="有效订单数量",
            normalized_phrase="有效订单数量",
            element_type=SemanticElementType.METRIC,
            element_id="effective_order_count",
            dataset_ids=("dataset",),
            source="name",
            priority=300,
        ),
        SemanticIndexEntry(
            id="entry-revenue-name",
            phrase="销售金额",
            normalized_phrase="销售金额",
            element_type=SemanticElementType.METRIC,
            element_id="revenue",
            dataset_ids=("dataset",),
            source="name",
            priority=300,
        ),
        SemanticIndexEntry(
            id="entry-revenue-alias",
            phrase="历史渠道营收合计",
            normalized_phrase="历史渠道营收合计",
            element_type=SemanticElementType.METRIC,
            element_id="revenue",
            dataset_ids=("dataset",),
            source="alias",
            priority=250,
        ),
    )
    return SemanticIndexSnapshot(
        id="idx-database-match",
        release_spec_hash="spec-database-match",
        content_hash="hash-database-match",
        state=IndexState.READY,
        embedding_model_id="test",
        vector_dimension=1,
        entries=entries,
        vectors=tuple((1.0,) for _item in entries),
    )


def test_llm_enabled_discovery_runs_embedding_in_every_map_mode(sales_release):
    """Parity source: ``EmbeddingMapper.accept`` returns true for LOOSE *or*
    Text2SQLType.LLM_OR_RULE, so a deployment with the LLM parser enabled recalls
    embeddings during STRICT/MODERATE discovery, not only in the final stage."""

    gateway = _ConstantEmbeddingGateway()
    index = SemanticIndexBuilder(gateway).build(sales_release)

    for mode in (MapMode.STRICT, MapMode.MODERATE):
        result = SemanticMapper(
            embedding_gateway=gateway,
            llm_enabled=True,
        ).map(
            question="业务表现",
            dataset_id="sales_dataset",
            index=index,
            mode=mode,
        )

        assert any(item.method is MatchMethod.EMBEDDING for item in result.matches), mode


def test_rule_only_discovery_keeps_embedding_out_of_strict_mapping(sales_release):
    """With Text2SQLType.ONLY_RULE upstream leaves EmbeddingMapper unaccepted."""

    gateway = _ConstantEmbeddingGateway()
    index = SemanticIndexBuilder(gateway).build(sales_release)

    result = SemanticMapper(embedding_gateway=gateway, llm_enabled=False).map(
        question="业务表现",
        dataset_id="sales_dataset",
        index=index,
        mode=MapMode.STRICT,
    )

    assert not any(item.method is MatchMethod.EMBEDDING for item in result.matches)


def test_term_expansion_runs_embedding_in_the_final_llm_stage(sales_release):
    """术语描述的二次映射必须与主路径同一个向量开关。

    上游 TermDescMapper 对术语描述重跑**全部** SchemaMapper,而 EmbeddingMapper
    的 accept 在 LLM_OR_RULE 阶段成立。我们主路径已对齐
    (mode is LOOSE or final_stage or llm_enabled),但术语分支只判 LOOSE,
    导致最终 LLM 阶段术语→指标的向量扩展整体丢失。
    """

    gateway = _ConstantEmbeddingGateway()
    index = SemanticIndexBuilder(gateway).build(sales_release)
    mapper = SemanticMapper(embedding_gateway=gateway)

    evidence = mapper.collect_evidence(
        question="销售额",
        dataset_ids=("sales_dataset",),
        index=index,
    )
    result = mapper.project_evidence(
        evidence=evidence,
        dataset_id="sales_dataset",
        mode=MapMode.STRICT,
        final_stage=True,
    )

    term_embedding = next(
        item
        for item in evidence.matches
        if item.channel is MappingEvidenceChannel.TERM_EMBEDDING
        and item.origin_term_entry_id is not None
    )
    assert term_embedding.detected_spans == ((0, 3),)
    assert term_embedding.detected_span_source == (f"term:{term_embedding.origin_term_entry_id}")
    assert next(item for item in result.matches if item.element_id == "net_revenue").method is (
        MatchMethod.TERM
    )


def test_database_match_counts_embedding_hits_as_registered(sales_release):
    """DatabaseMatch 的两处判定都必须读全量已有匹配,而不是只读字典命中。

    上游 DatabaseMatchStrategy.getThreshold 检查的是整个 mapInfo(此时
    EmbeddingMapper 已先写入匹配),KeywordMapper.getRegElementSet 同理。
    我们只看字典命中,于是「向量有命中、字典没命中」时:
    - 阈值仍被减半 -> 多召回一批低质库名匹配;
    - 去重集合漏掉已命中元素 -> 同一元素被再加一条,detected_text 可能退化成
      更差的片段,连带影响候选打分(按 len(detected_text) * score 计)。
    """

    gateway = _ConstantEmbeddingGateway()
    index = SemanticIndexBuilder(gateway).build(sales_release)

    mapper = SemanticMapper(embedding_gateway=gateway)
    evidence = mapper.collect_evidence(
        question="净收",
        dataset_ids=("sales_dataset",),
        index=index,
    )
    result = mapper.project_evidence(
        evidence=evidence,
        dataset_id="sales_dataset",
        mode=MapMode.MODERATE,
        final_stage=True,
    )

    target_evidence = [item for item in evidence.matches if item.element_id == "net_revenue"]
    assert {item.channel for item in target_evidence} >= {
        MappingEvidenceChannel.EMBEDDING,
        MappingEvidenceChannel.DATABASE,
    }
    target = next(item for item in result.matches if item.element_id == "net_revenue")
    assert target.method is MatchMethod.EMBEDDING


def test_database_projection_registration_is_typed_for_cross_family_same_ids() -> None:
    """A metric hit cannot suppress a dimension whose family-local ID is equal."""

    existing = SchemaMatch(
        entry_id="metric-entry",
        dataset_id="dataset",
        element_type=SemanticElementType.METRIC,
        element_id="shared",
        phrase="成交",
        detected_text="成交",
        method=MatchMethod.EXACT,
        score=1.0,
        priority=300,
    )
    database_evidence = MappingEvidenceMatch(
        entry_id="dimension-entry",
        eligible_dataset_ids=("dataset",),
        element_type=SemanticElementType.DIMENSION,
        element_id="shared",
        phrase="订单成交日期",
        normalized_phrase="订单成交日期",
        detected_text="成交",
        method=MatchMethod.KEYWORD,
        score=0.5,
        priority=300,
        channel=MappingEvidenceChannel.DATABASE,
        entry_source="name",
    )

    projected = SemanticMapper()._project_database_evidence(
        (database_evidence,),
        dataset_id="dataset",
        mode=MapMode.MODERATE,
        channel=MappingEvidenceChannel.DATABASE,
        existing_matches=[existing],
    )

    assert [(item.element_type, item.element_id) for item in projected] == [
        (SemanticElementType.DIMENSION, "shared")
    ]


@pytest.mark.parametrize(
    ("question", "scope_phrase", "metric_phrase", "fragment", "date_phrase", "count_phrase"),
    (
        (
            "各电商平台的活动交易额",
            "电商活动交易额",
            "交易额",
            "活动",
            "活动日期",
            "参加活动商家数量",
        ),
        (
            "各渠道平台的促销销售额",
            "渠道促销销售额",
            "销售额",
            "促销",
            "促销日期",
            "参加促销商家数量",
        ),
    ),
)
def test_moderate_mapping_drops_partial_fragment_covered_by_a_longer_detected_span(
    question: str,
    scope_phrase: str,
    metric_phrase: str,
    fragment: str,
    date_phrase: str,
    count_phrase: str,
) -> None:
    definitions = (
        ("scope", scope_phrase, SemanticElementType.DATASET),
        ("amount", metric_phrase, SemanticElementType.METRIC),
        ("date", date_phrase, SemanticElementType.DIMENSION),
        ("merchant_count", count_phrase, SemanticElementType.METRIC),
    )
    entries = tuple(
        SemanticIndexEntry(
            id=f"entry-{element_id}",
            phrase=phrase,
            normalized_phrase=phrase,
            element_type=element_type,
            element_id=element_id,
            dataset_ids=("business",),
            source="name",
            priority=300,
        )
        for element_id, phrase, element_type in definitions
    )
    index = SemanticIndexSnapshot(
        id="idx-covered-partial",
        release_spec_hash="spec-covered-partial",
        content_hash="hash-covered-partial",
        state=IndexState.READY,
        embedding_model_id="test",
        vector_dimension=1,
        entries=entries,
        vectors=tuple((1.0,) for _item in entries),
    )

    result = SemanticMapper().map(
        question=question,
        dataset_id="business",
        index=index,
        mode=MapMode.MODERATE,
    )

    by_id = {item.element_id: item for item in result.matches}
    assert by_id["amount"].method is MatchMethod.EXACT
    assert by_id["scope"].detected_text.endswith(f"{fragment}{metric_phrase}")
    assert "date" not in by_id
    assert "merchant_count" not in by_id


@pytest.mark.parametrize(
    ("question", "scope_phrase", "metric_phrase", "fragment", "date_phrase"),
    (
        (
            "各活动的活动交易额",
            "电商活动交易额",
            "交易额",
            "活动",
            "活动日期",
        ),
        (
            "各促销的促销销售额",
            "渠道促销销售额",
            "销售额",
            "促销",
            "促销日期",
        ),
    ),
)
def test_moderate_mapping_keeps_partial_fragment_with_an_independent_span(
    question: str,
    scope_phrase: str,
    metric_phrase: str,
    fragment: str,
    date_phrase: str,
) -> None:
    """CANDIDATE_DISCOVERY only drops a partial hit covered at the same span.

    This protects the reviewed Mapper contract: a fragment nested inside a
    longer hit is not a second intent, while another occurrence of that same
    fragment elsewhere in the question remains valid evidence for the existing
    ambiguity/LLM settlement stages.
    """

    definitions = (
        ("scope", scope_phrase, SemanticElementType.DATASET),
        ("amount", metric_phrase, SemanticElementType.METRIC),
        ("date", date_phrase, SemanticElementType.DIMENSION),
        ("merchant_count", f"参加{fragment}商家数量", SemanticElementType.METRIC),
    )
    entries = tuple(
        SemanticIndexEntry(
            id=f"entry-{element_id}",
            phrase=phrase,
            normalized_phrase=phrase,
            element_type=element_type,
            element_id=element_id,
            dataset_ids=("business",),
            source="name",
            priority=300,
        )
        for element_id, phrase, element_type in definitions
    )
    index = SemanticIndexSnapshot(
        id="idx-independent-partial",
        release_spec_hash="spec-independent-partial",
        content_hash="hash-independent-partial",
        state=IndexState.READY,
        embedding_model_id="test",
        vector_dimension=1,
        entries=entries,
        vectors=tuple((1.0,) for _item in entries),
    )

    result = SemanticMapper().map(
        question=question,
        dataset_id="business",
        index=index,
        mode=MapMode.MODERATE,
    )

    by_id = {item.element_id: item for item in result.matches}
    assert by_id["amount"].method is MatchMethod.EXACT
    assert by_id["scope"].detected_text.endswith(f"{fragment}{metric_phrase}")
    assert by_id["date"].detected_text == fragment
