from __future__ import annotations

from knowflow_analytics.query.contracts import MapMode, MatchMethod
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.semantic.index import (
    EmbeddingBatch,
    IndexState,
    SemanticElementType,
    SemanticIndexEntry,
    SemanticIndexSnapshot,
)


class _CountingEmbeddingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        self.calls.append(texts)
        return EmbeddingBatch(
            model_id="global-mapper-test",
            dimension=2,
            vectors=tuple((1.0, 0.0) for _text in texts),
        )


class _CrossScopeOrderAmountGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        vectors = []
        for text in texts:
            if text in {"订单额", "订单净金额"}:
                vectors.append((1.0, 0.0))
            elif text == "订单明细条数":
                vectors.append((0.0, 1.0))
            else:
                vectors.append((-1.0, 0.0))
        return EmbeddingBatch(
            model_id="global-mapper-test",
            dimension=2,
            vectors=tuple(vectors),
        )


def _entry(
    entry_id: str,
    phrase: str,
    element_type: SemanticElementType,
    element_id: str,
    dataset_ids: tuple[str, ...],
    *,
    source: str = "name",
    priority: int = 300,
    description: str = "",
    dimension_id: str | None = None,
    raw_value: object | None = None,
) -> SemanticIndexEntry:
    return SemanticIndexEntry(
        id=entry_id,
        phrase=phrase,
        normalized_phrase=phrase.casefold().replace(" ", ""),
        element_type=element_type,
        element_id=element_id,
        dataset_ids=dataset_ids,
        source=source,
        priority=priority,
        description=description,
        dimension_id=dimension_id,
        raw_value=raw_value,
    )


def _index(
    entries: tuple[SemanticIndexEntry, ...],
    *,
    vectors: tuple[tuple[float, float], ...] | None = None,
) -> SemanticIndexSnapshot:
    return SemanticIndexSnapshot(
        id="idx-global-mapper",
        release_spec_hash="spec-global-mapper",
        content_hash="hash-global-mapper",
        state=IndexState.READY,
        embedding_model_id="global-mapper-test",
        vector_dimension=2,
        entries=entries,
        vectors=vectors or tuple((1.0, 0.0) for _entry in entries),
    )


def _match_signature(result) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.entry_id,
            item.element_type,
            item.element_id,
            item.phrase,
            item.detected_text,
            item.method,
            item.score,
            item.dimension_id,
            item.raw_value,
        )
        for item in result.matches
    )


def test_collects_embedding_and_term_evidence_once_for_every_scope_and_view() -> None:
    """Reviewed Mapper contract: retrieval is global; MapMode remains a projection.

    CANDIDATE_DISCOVERY may project STRICT/MODERATE/LOOSE for several QueryScopes,
    and FINAL_PARSING may project final/ALL afterward.  Those views must reuse one
    question/term embedding batch instead of calling the model per Scope or mode.
    """

    entries = (
        _entry(
            "entry-term",
            "经营表现",
            SemanticElementType.TERM,
            "term-performance",
            ("scope-a", "scope-b"),
            description="收益情况",
        ),
        _entry(
            "entry-revenue-a",
            "净收入",
            SemanticElementType.METRIC,
            "revenue-a",
            ("scope-a",),
        ),
        _entry(
            "entry-revenue-b",
            "营业收入",
            SemanticElementType.METRIC,
            "revenue-b",
            ("scope-b",),
        ),
    )
    gateway = _CountingEmbeddingGateway()
    mapper = SemanticMapper(embedding_gateway=gateway)

    evidence = mapper.collect_evidence(
        question="经营表现",
        dataset_ids=("scope-a", "scope-b"),
        index=_index(entries),
    )

    assert len(gateway.calls) == 1
    assert {"经营表现", "收益情", "情况"} <= set(gateway.calls[0])
    # 「经营表」「表现」是精确命中「经营表现」的碎片，不再单独去查向量。
    assert {"经营表", "表现"}.isdisjoint(gateway.calls[0])

    mapper.project_evidence(evidence=evidence, dataset_id="scope-a", mode=MapMode.STRICT)
    mapper.project_evidence(evidence=evidence, dataset_id="scope-b", mode=MapMode.MODERATE)
    mapper.project_evidence(evidence=evidence, dataset_id="scope-a", mode=MapMode.LOOSE)
    final = mapper.project_evidence(
        evidence=evidence,
        dataset_id="scope-a",
        mode=MapMode.STRICT,
        final_stage=True,
    )
    all_view = mapper.project_evidence(
        evidence=evidence,
        dataset_id="scope-b",
        mode=MapMode.ALL,
        final_stage=True,
    )

    assert len(gateway.calls) == 1
    assert next(item for item in final.matches if item.element_id == "revenue-a").method is (
        MatchMethod.TERM
    )
    assert {item.element_id for item in all_view.matches} >= {"revenue-b"}


def test_adding_an_unrelated_scope_cannot_starve_a_projection_from_global_top_k() -> None:
    """Raw evidence is never globally truncated before the Scope projection."""

    target = _entry(
        "entry-target",
        "目标指标",
        SemanticElementType.METRIC,
        "target",
        ("scope-a",),
        priority=100,
    )
    noise = tuple(
        _entry(
            f"entry-noise-{index}",
            f"噪声指标{index}",
            SemanticElementType.METRIC,
            f"noise-{index}",
            ("scope-noise",),
            priority=1_000,
        )
        for index in range(12)
    )
    gateway = _CountingEmbeddingGateway()
    mapper = SemanticMapper(embedding_gateway=gateway)
    index = _index((target, *noise))

    base = mapper.collect_evidence(
        question="业务情况",
        dataset_ids=("scope-a",),
        index=index,
    )
    expanded = mapper.collect_evidence(
        question="业务情况",
        dataset_ids=("scope-a", "scope-noise"),
        index=index,
    )

    base_view = mapper.project_evidence(
        evidence=base,
        dataset_id="scope-a",
        mode=MapMode.LOOSE,
    )
    expanded_view = mapper.project_evidence(
        evidence=expanded,
        dataset_id="scope-a",
        mode=MapMode.LOOSE,
    )

    assert _match_signature(expanded_view) == _match_signature(base_view)
    assert {item.element_id for item in expanded_view.matches} == {"target"}


def test_scope_canonical_entries_are_not_deduplicated_before_projection() -> None:
    """One semantic ID may have a different governed canonical entry per Scope."""

    entries = (
        _entry(
            "entry-shared-name",
            "销售额",
            SemanticElementType.METRIC,
            "shared-revenue",
            ("scope-a", "scope-b"),
        ),
        _entry(
            "entry-scope-a-name",
            "甲口径销售额",
            SemanticElementType.METRIC,
            "shared-revenue",
            ("scope-a",),
            source="scope_name",
            priority=325,
        ),
        _entry(
            "entry-scope-b-name",
            "乙口径销售额",
            SemanticElementType.METRIC,
            "shared-revenue",
            ("scope-b",),
            source="scope_name",
            priority=325,
        ),
    )
    mapper = SemanticMapper()
    evidence = mapper.collect_evidence(
        question="甲口径销售额和乙口径销售额",
        dataset_ids=("scope-a", "scope-b"),
        index=_index(entries),
    )

    canonical = {
        item.entry_id: item.eligible_dataset_ids
        for item in evidence.matches
        if item.entry_id in {"entry-scope-a-name", "entry-scope-b-name"}
        and item.origin_term_entry_id is None
        and item.method is MatchMethod.EXACT
    }
    scope_a = mapper.project_evidence(
        evidence=evidence,
        dataset_id="scope-a",
        mode=MapMode.STRICT,
    )
    scope_b = mapper.project_evidence(
        evidence=evidence,
        dataset_id="scope-b",
        mode=MapMode.STRICT,
    )

    assert canonical == {
        "entry-scope-a-name": ("scope-a",),
        "entry-scope-b-name": ("scope-b",),
    }
    assert next(item for item in scope_a.matches if item.element_id == "shared-revenue").phrase == (
        "甲口径销售额"
    )
    assert next(item for item in scope_b.matches if item.element_id == "shared-revenue").phrase == (
        "乙口径销售额"
    )


def test_term_and_dimension_value_evidence_keep_their_eligible_scope_intersection() -> None:
    """Term description targets use term∩target Scope; values keep all governed Scopes."""

    entries = (
        _entry(
            "entry-term",
            "销售额",
            SemanticElementType.TERM,
            "term-sales",
            ("scope-a", "scope-b"),
            description="净收入",
        ),
        _entry(
            "entry-net-revenue",
            "净收入",
            SemanticElementType.METRIC,
            "net-revenue",
            ("scope-a",),
        ),
        _entry(
            "entry-east",
            "华东",
            SemanticElementType.DIMENSION_VALUE,
            "region-east",
            ("scope-a", "scope-b"),
            source="value",
            dimension_id="region",
            raw_value="EAST",
        ),
    )
    mapper = SemanticMapper()
    evidence = mapper.collect_evidence(
        question="销售额 华东",
        dataset_ids=("scope-a", "scope-b"),
        index=_index(entries),
    )

    term_targets = [
        item
        for item in evidence.matches
        if item.entry_id == "entry-net-revenue" and item.origin_term_entry_id == "entry-term"
    ]
    values = [
        item
        for item in evidence.matches
        if item.entry_id == "entry-east" and item.origin_term_entry_id is None
    ]
    scope_a = mapper.project_evidence(
        evidence=evidence,
        dataset_id="scope-a",
        mode=MapMode.STRICT,
    )
    scope_b = mapper.project_evidence(
        evidence=evidence,
        dataset_id="scope-b",
        mode=MapMode.STRICT,
    )

    assert term_targets
    assert {item.eligible_dataset_ids for item in term_targets} == {("scope-a",)}
    assert values
    assert {item.eligible_dataset_ids for item in values} == {("scope-a", "scope-b")}
    assert next(item for item in scope_a.matches if item.element_id == "net-revenue").method is (
        MatchMethod.TERM
    )
    east_a = next(item for item in scope_a.matches if item.element_id == "region-east")
    east_b = next(item for item in scope_b.matches if item.element_id == "region-east")
    assert (east_a.dimension_id, east_a.raw_value, east_a.method) == (
        "region",
        "EAST",
        MatchMethod.EXACT,
    )
    assert (east_b.dimension_id, east_b.raw_value, east_b.method) == (
        "region",
        "EAST",
        MatchMethod.EXACT,
    )
    assert "net-revenue" not in {item.element_id for item in scope_b.matches}


def test_global_longest_exact_phrase_suppresses_nested_scope_local_metric() -> None:
    mapper = SemanticMapper()
    evidence = mapper.collect_evidence(
        question="美国的入驻电商数量占比多少",
        dataset_ids=("merchant_scope", "platform_scope"),
        index=_index(
            (
                _entry(
                    "entry-onboarded-platforms",
                    "入驻电商数量",
                    SemanticElementType.METRIC,
                    "onboarded_platform_count",
                    ("merchant_scope",),
                ),
                _entry(
                    "entry-platform-count",
                    "电商数量",
                    SemanticElementType.METRIC,
                    "platform_count",
                    ("platform_scope",),
                ),
            )
        ),
        include_embeddings=False,
    )

    exact_metrics = {
        item.element_id
        for item in evidence.matches
        if item.element_type is SemanticElementType.METRIC and item.method is MatchMethod.EXACT
    }
    assert exact_metrics == {"onboarded_platform_count"}


def test_global_embedding_long_phrase_suppresses_nested_metric_in_another_scope() -> None:
    entries = (
        _entry(
            "entry-net-amount",
            "订单净金额",
            SemanticElementType.METRIC,
            "net_amount",
            ("orders",),
        ),
        _entry(
            "entry-item-count",
            "订单明细条数",
            SemanticElementType.METRIC,
            "item_count",
            ("order_items",),
        ),
    )
    gateway = _CrossScopeOrderAmountGateway()
    mapper = SemanticMapper(embedding_gateway=gateway, llm_enabled=True)
    evidence = mapper.collect_evidence(
        question="直营渠道订单额占比多少",
        dataset_ids=("orders", "order_items"),
        index=_index(entries, vectors=((1.0, 0.0), (0.0, 1.0))),
    )

    assert any(
        item.element_id == "net_amount"
        and item.detected_text == "订单额"
        and item.method is MatchMethod.EMBEDDING
        and item.detected_spans == ((4, 7),)
        for item in evidence.matches
    )
    assert not any(
        item.element_id == "item_count"
        and item.detected_text == "订单"
        and item.method is MatchMethod.KEYWORD
        for item in evidence.matches
    )
    item_view = mapper.project_evidence(
        evidence=evidence,
        dataset_id="order_items",
        mode=MapMode.MODERATE,
    )
    assert "item_count" not in {item.element_id for item in item_view.matches}


def test_global_long_surface_is_type_agnostic_before_typed_adjudication() -> None:
    entries = (
        _entry(
            "entry-order-amount-dimension",
            "订单净金额",
            SemanticElementType.DIMENSION,
            "order_amount_dimension",
            ("orders",),
        ),
        _entry(
            "entry-item-count",
            "订单明细条数",
            SemanticElementType.METRIC,
            "item_count",
            ("order_items",),
        ),
    )
    gateway = _CrossScopeOrderAmountGateway()
    evidence = SemanticMapper(
        embedding_gateway=gateway,
        llm_enabled=True,
    ).collect_evidence(
        question="直营渠道订单额占比多少",
        dataset_ids=("orders", "order_items"),
        index=_index(entries, vectors=((1.0, 0.0), (0.0, 1.0))),
    )

    assert any(
        item.element_id == "order_amount_dimension" and item.method is MatchMethod.EMBEDDING
        for item in evidence.matches
    )
    assert not any(
        item.element_id == "item_count"
        and item.detected_text == "订单"
        and item.method is MatchMethod.KEYWORD
        for item in evidence.matches
    )


def test_global_embedding_cover_retains_an_independent_short_occurrence() -> None:
    entries = (
        _entry(
            "entry-net-amount",
            "订单净金额",
            SemanticElementType.METRIC,
            "net_amount",
            ("orders",),
        ),
        _entry(
            "entry-item-count",
            "订单明细条数",
            SemanticElementType.METRIC,
            "item_count",
            ("order_items",),
        ),
    )
    gateway = _CrossScopeOrderAmountGateway()
    evidence = SemanticMapper(
        embedding_gateway=gateway,
        llm_enabled=True,
    ).collect_evidence(
        question="订单并且订单额分别多少",
        dataset_ids=("orders", "order_items"),
        index=_index(entries, vectors=((1.0, 0.0), (0.0, 1.0))),
    )

    short = next(
        item
        for item in evidence.matches
        if item.element_id == "item_count"
        and item.detected_text == "订单"
        and item.method is MatchMethod.KEYWORD
    )
    long = next(
        item
        for item in evidence.matches
        if item.element_id == "net_amount"
        and item.detected_text == "订单额"
        and item.method is MatchMethod.EMBEDDING
    )
    assert short.detected_spans == ((0, 2), (4, 6))
    assert long.detected_spans == ((4, 7),)
