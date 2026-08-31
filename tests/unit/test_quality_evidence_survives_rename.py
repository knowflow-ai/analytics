from __future__ import annotations

from knowflow_analytics.contracts import AnalysisTopicRouteSpec, SemanticContextEntry
from knowflow_analytics.hashing import semantic_evidence_hash, semantic_release_hash
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog


def test_rename_changes_spec_hash_but_not_evidence_hash(sales_release) -> None:
    """这是 B5 的核心断言，用来固定住两个哈希的分工。

    spec_hash 负责版本追溯（任何改动都要变），evidence_hash 负责判断
    需要扫库的证据是否还成立（只有影响正确性的改动才变）。
    """

    renamed = sales_release.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(update={"name": f"{item.name} 改名"})
                for item in sales_release.metrics
            )
        }
    )

    assert semantic_release_hash(renamed) != semantic_release_hash(sales_release)
    assert semantic_evidence_hash(renamed) == semantic_evidence_hash(sales_release)


def test_join_condition_change_moves_both_hashes(sales_release) -> None:
    """Join 条件决定物理 SQL，两个哈希都必须变。"""

    if not sales_release.relations:
        return
    changed = sales_release.model_copy(
        update={
            "relations": tuple(
                item.model_copy(update={"join_type": "inner"}) for item in sales_release.relations
            )
        }
    )
    assert semantic_release_hash(changed) != semantic_release_hash(sales_release)
    assert semantic_evidence_hash(changed) != semantic_evidence_hash(sales_release)


def test_dataset_scope_change_moves_the_evidence_hash(sales_release) -> None:
    """主题成员范围决定可问范围和可达性，属于正确性相关。"""

    if not sales_release.datasets:
        return
    trimmed = sales_release.model_copy(
        update={
            "datasets": tuple(
                item.model_copy(update={"metric_ids": item.metric_ids[:1]})
                for item in sales_release.datasets
            )
        }
    )
    assert semantic_evidence_hash(trimmed) != semantic_evidence_hash(sales_release)


def test_context_change_moves_release_hash_without_invalidating_database_evidence(
    sales_release,
) -> None:
    """Context changes LLM interpretation and must version the Release/eval, but
    it does not change joins, expressions or data quality evidence that requires
    an expensive database scan. Legacy route context follows the same rule."""

    entry = SemanticContextEntry(
        id="ctx-project",
        target_type="project",
        target_id=sales_release.project_id,
        kind="convention",
        text="金额统一使用人民币。",
        source_type="human_convention",
    )
    base_route = AnalysisTopicRouteSpec(
        dataset_id=sales_release.datasets[0].id,
        root_model_id="orders",
    )
    route = base_route.model_copy(update={"ai_context": "净收入已扣除退款。"})
    base = sales_release.model_copy(update={"analysis_topic_routes": (base_route,)})
    changed = base.model_copy(
        update={"semantic_context": (entry,), "analysis_topic_routes": (route,)}
    )

    assert semantic_release_hash(changed) != semantic_release_hash(base)
    assert semantic_evidence_hash(changed) == semantic_evidence_hash(base)


def test_compiled_catalog_context_does_not_leak_back_into_database_evidence_hash(
    sales_catalog,
) -> None:
    """Production releases repeat Catalog fields inside ``modeling_catalog``."""

    base = compile_semantic_catalog(sales_catalog)
    entry = SemanticContextEntry(
        id="ctx-compiled-project",
        target_type="project",
        target_id=sales_catalog.project_id,
        kind="convention",
        text="金额统一使用人民币。",
        source_type="human_convention",
    )
    routes = tuple(
        item.model_copy(update={"ai_context": "净收入已扣除退款。"})
        for item in sales_catalog.analysis_topic_routes
    )
    changed_catalog = sales_catalog.model_copy(
        update={"semantic_context": (entry,), "analysis_topic_routes": routes}
    )
    changed = compile_semantic_catalog(changed_catalog)

    assert semantic_release_hash(changed) != semantic_release_hash(base)
    assert semantic_evidence_hash(changed) == semantic_evidence_hash(base)
