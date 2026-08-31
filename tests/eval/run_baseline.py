"""AI 建模评测基线。

    # 只跑规则（S1 画像 + S3 排除法），不需要模型：
    .venv/bin/python -m tests.eval.run_baseline --rules-only

    # 跑完整工作流，用 start_analytics.sh 同样的环境变量连 RAGFlow 模型网关：
    .venv/bin/python -m tests.eval.run_baseline --workflow staged --runs 5
    .venv/bin/python -m tests.eval.run_baseline --workflow single_call --runs 5

输出每列的 kind / aggregation 是否与人工标注一致，按表和按错误类型汇总。
改 prompt 前后各跑一次，数字进 commit message。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.classify import classify_table, rule_based_role
from knowflow_analytics.modeling.revision import RevisionEditor
from knowflow_analytics.modeling.rule_modeller import RuleSemanticModeller
from knowflow_analytics.modeling.topology import build_topology
from tests.eval.fixtures import LABELS, PROFILES, SNAPSHOT


def _rules_only() -> dict[tuple[str, str], tuple[str, str | None]]:
    topology = build_topology(SNAPSHOT)
    out = {}
    for table in SNAPSHOT.tables:
        key = (table.schema_name, table.name)
        fk_cols = frozenset(c for fk in table.foreign_keys for c in fk.constrained_columns)
        role = rule_based_role(
            table,
            in_degree=topology[key].in_degree,
            out_degree=topology[key].out_degree,
            prefills_numeric_non_key=0,
        )
        for p in classify_table(
            table, role=role, profile=PROFILES[key], foreign_key_columns=fk_cols
        ):
            out[(table.name, p.column)] = (
                p.kind.value,
                p.aggregation.value if p.aggregation else None,
            )
    return out


def _gateway():
    from knowflow_analytics.gateways.model import HttpModelGateway

    base = os.environ.get("KNOWFLOW_ANALYTICS_RAGFLOW_BASE_URL")
    token = os.environ.get("KNOWFLOW_ANALYTICS_RAGFLOW_SERVICE_TOKEN")
    tenant = os.environ.get("KNOWFLOW_ANALYTICS_EVAL_TENANT_ID", "")
    llm = os.environ.get("KNOWFLOW_ANALYTICS_EVAL_LLM_ID", "")
    if not base or not token:
        sys.exit(
            "需要 KNOWFLOW_ANALYTICS_RAGFLOW_BASE_URL / _SERVICE_TOKEN（见 start_analytics.sh）"
        )
    return HttpModelGateway(
        base_url=base, service_token=token, tenant_id=tenant, llm_id=llm, timeout_seconds=240
    )


def _workflow(workflow: str) -> dict[tuple[str, str], tuple[str, str | None]]:
    result = RuleSemanticModeller().build(project_id="eval", snapshot=SNAPSHOT)
    revision = RevisionEditor().create(
        project_id="eval",
        schema_snapshot_hash=SNAPSHOT.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=(),
    )
    patches = AiSemanticModeller(model_gateway=_gateway(), workflow=workflow).suggest(
        modeling_job_id="eval",
        revision=revision,
        snapshot=SNAPSHOT,
        profiles=PROFILES,
        tenant_id=os.environ.get("KNOWFLOW_ANALYTICS_EVAL_TENANT_ID", ""),
    )
    fields = {f.id: f for f in revision.semantic_spec.fields}
    models = {m.id: m for m in revision.semantic_spec.models}
    out = {}
    for p in patches:
        if p.target_kind != "field":
            continue
        f = fields[p.target_id]
        out[(models[f.model_id].table, f.column)] = (
            p.changes.get("kind", "field"),
            p.changes.get("aggregation"),
        )
    return out


def _score(predicted) -> None:
    by_table: dict[str, Counter] = {}
    errors: list[str] = []
    for (table, column), (kind, agg) in LABELS.items():
        got_kind, got_agg = predicted.get((table, column), ("<missing>", None))
        c = by_table.setdefault(table, Counter())
        c["total"] += 1
        kind_ok = got_kind == kind
        agg_ok = (got_agg or None) == (agg or None) if kind == "measure" else True
        if kind_ok and agg_ok:
            c["ok"] += 1
        else:
            tag = "kind" if not kind_ok else "agg"
            c[f"err_{tag}"] += 1
            if got_kind == "measure" and kind != "measure":
                c["silent_wrong_number"] += 1
            errors.append(
                f"  {table}.{column:<16} 期望 {kind}/{agg or '-':<5} "
                f"实际 {got_kind}/{got_agg or '-'}"
            )
    total = sum(c["total"] for c in by_table.values())
    ok = sum(c["ok"] for c in by_table.values())
    silent = sum(c["silent_wrong_number"] for c in by_table.values())
    print(f"\n总计 {ok}/{total} 正确（{ok / total:.0%}）；静默错数（非度量被标成度量）{silent} 列")
    for table, c in by_table.items():
        print(
            f"  {table:<10} {c['ok']}/{c['total']}  kind 错 {c['err_kind']}  agg 错 {c['err_agg']}"
            f"  静默错数 {c['silent_wrong_number']}"
        )
    if errors:
        print("\n错误明细：")
        print("\n".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--workflow", choices=["staged", "single_call"])
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    if args.rules_only:
        print("== 规则（S1 画像 + S3 排除法），不调模型 ==")
        _score(_rules_only())
        return
    if not args.workflow:
        parser.error("--rules-only 或 --workflow 二选一")
    for run in range(1, args.runs + 1):
        print(f"\n== workflow={args.workflow} run {run}/{args.runs} ==")
        _score(_workflow(args.workflow))


if __name__ == "__main__":
    main()
