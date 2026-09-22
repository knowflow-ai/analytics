"""问数链路重放基准：把真实问句重新放一遍，量行为变化与静默错答。

改问数核心前后各跑一次，数字进方案与 commit message。

    BENCH=/tmp/bench/query_replay_benchmark.py
    SUITE=/tmp/bench/query_suites/acct.json

    # 冻结基线（跑容器里正在服役的代码），再跑一遍同样的东西量噪声地板
    python $BENCH --suite $SUITE --runs 3 --out base_a.json
    python $BENCH --suite $SUITE --runs 3 --out base_b.json
    # 改完之后
    PYTHONPATH=/tmp/newsrc python $BENCH --suite $SUITE --runs 3 --out after.json
    # 对比（噪声地板一起给，才知道差异是不是真的）
    python $BENCH --diff base_a.json after.json --noise base_b.json

判据分两层：
①**指纹**（全部题，不需要真值）——state / 作用域 / 错误码 / 诊断 severity / 列清单 /
  行数 / all_null / S2SQL 原文。它回答"有没有东西变了"，重构的风险大半在这里。
②**真值**（写了 truth_sql 的题）——直连客户库跑参考 SQL，比数字。
  它回答"变好了还是变坏了"。

结局六种，按"用户被骗的程度"排序，`refuse` 高于 `empty` 与 `wrong`——
拒答用户知道没拿到答案，后两者他以为拿到了：

    correct   数字对上
    answered  有数据但这题没给真值
    clarify   弹了澄清卡
    refuse    拒答
    empty     0 行或全 NULL 而真值有数据 —— 假的"没有数据"，一句关于用户业务的假话
    wrong     有数据但数字不对 —— **静默错答**，0 底线那一项

题集里 `expect: "fingerprint"` 的题不进正确率汇总：它们没有真值，0 行有可能正是
正确答案（「是否借贷平衡」全平时就该 0 行），混进来会把对的记成假空。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import perf_counter
from typing import Any

_NUMERIC_TOLERANCE = Decimal("0.01")


def _load_application():
    """拿到与生产完全相同的 application：spy 掉 create_api 把它截下来。

    不复制 create_app 的构造逻辑——复制出来的装配迟早与生产漂移，
    那时基准量的就不是线上那条链路了。
    """
    import knowflow_analytics.server as srv

    captured: dict[str, Any] = {}
    real = srv.create_api

    def _spy(*, application, **kwargs):
        captured["app"] = application
        return real(application=application, **kwargs)

    srv.create_api = _spy
    srv.create_app()
    srv.create_api = real
    return captured["app"]


def _truth_rows(app, project_id: str, sql: str) -> tuple[tuple[Any, ...], ...]:
    """参考 SQL 直连客户库。真值必须来自库，不能来自另一次问数。"""
    from sqlalchemy import text

    binding = app._sources.for_project(project_id)
    if binding.engine is None:
        raise RuntimeError("该项目的数据源没有引擎，拿不到真值")
    with binding.engine.connect() as conn:
        return tuple(tuple(row) for row in conn.execute(text(sql)).fetchall())


def _numbers(rows: tuple[tuple[Any, ...], ...]) -> list[Decimal]:
    """把结果里的数字摘出来排序。

    按「数字多重集 + 行数」比而不是逐列比：两边列名不同（业务名 vs 物理名）、
    列序也不保证一致，硬比会把对的判成错的。数字对上、行数对上，答案就是对的；
    这也正是人工核对时实际在做的事。
    """
    out: list[Decimal] = []
    for row in rows:
        for cell in row:
            if isinstance(cell, bool) or cell is None:
                continue
            try:
                out.append(round(Decimal(str(cell)), 2))
            except (InvalidOperation, ValueError):
                continue
    return sorted(out)


def _texts(rows: tuple[tuple[Any, ...], ...]) -> list[str]:
    out: list[str] = []
    for row in rows:
        for cell in row:
            if isinstance(cell, str) and cell.strip():
                try:
                    Decimal(cell)
                except (InvalidOperation, ValueError):
                    out.append(cell.strip())
    return sorted(out)


def _blank(rows: tuple[tuple[Any, ...], ...]) -> bool:
    """「没有数据」的两种长相是同一件事。

    MySQL 里 `SUM(x) WHERE 无匹配` 给**一行 NULL**，而 `SELECT 列 WHERE 无匹配` 给
    **零行**。两者都是"这个条件下没有记录"，用户看到的也是同一句话。按行数硬比会把
    它们判成不同（实测 D013 因此被记成 `wrong`，而系统其实诚实地答了"没有数据"）。
    """

    return not rows or all(cell is None for row in rows for cell in row)


def _matches_truth(actual: tuple[tuple[Any, ...], ...], truth: tuple[tuple[Any, ...], ...]) -> bool:
    """行数相同，且真值的每个数字都能在答案里找到。

    **是包含而不是相等**：分组键的表示法两边合法地不同——真值按 `period='2024-01'`
    分组，模型按 `月份=1` 分组，两个都对，但后者会多出 1、3 这些数字。要求相等会把
    对的答案判成错的（实测 acct-05 就是这么被误判的）。代价是查不出「多投影了一列
    无关数字」，那是已知的良性差异（CLAUDE.md 2026-09-05：多投影一列绝对值，
    占比数字逐位相同），远不如错数字重要。
    """
    if _blank(actual) or _blank(truth):
        return _blank(actual) and _blank(truth)
    if len(actual) != len(truth):
        return False
    got = _numbers(actual)
    return all(any(abs(a - b) <= _NUMERIC_TOLERANCE for a in got) for b in _numbers(truth))


def _fingerprint(dump: dict[str, Any]) -> dict[str, Any]:
    """一次运行留下的可比痕迹。不含耗时——耗时波动大，不该让它制造假差异。"""
    data = dump.get("data") or {}
    rows = data.get("rows") or []
    semantic = dump.get("semantic_query") or {}
    error = dump.get("error") or {}
    diagnostics = dump.get("diagnostics") or {}
    # 诊断是单个对象（category/severity/summary），不是列表也没有 code 字段。
    # severity 必须进指纹：「零行聚合算出的空值」这类告警下 state 仍是 COMPLETED，
    # 光看 state 和数字分不出「答对了」和「答了个空值」。
    diagnosis = ""
    if diagnostics:
        diagnosis = f"{diagnostics.get('category')}/{diagnostics.get('severity')}"
    return {
        "state": dump.get("state"),
        "dataset_id": semantic.get("dataset_id"),
        "code": error.get("code") or None,
        "diagnosis": diagnosis,
        "columns": tuple(data.get("columns") or ()),
        "row_count": len(rows),
        "all_null": bool(rows) and all(cell is None for row in rows for cell in row),
        "s2sql": dump.get("corrected_s2sql"),
        "options": tuple(sorted(str(o.get("label")) for o in (dump.get("options") or []))),
    }


def _classify(dump: dict[str, Any], truth: tuple[tuple[Any, ...], ...] | None) -> str:
    state = dump.get("state")
    if state == "CLARIFICATION_REQUIRED":
        return "clarify"
    if state != "COMPLETED":
        return "refuse"
    rows = tuple(tuple(r) for r in ((dump.get("data") or {}).get("rows") or []))
    blank = _blank(rows)
    if truth is None:
        return "empty" if blank else "answered"
    truth_blank = _blank(truth)
    if truth_blank:
        # 真值本身没有数据（demo 库缺这个期间/科目）。这道题考的是**零行诚实性**：
        # 必须说"没有数据"，不能给 0 也不能编一个数字。
        return "correct" if blank else "wrong"
    if blank:
        # 真值有数据、答案是 0 行或全 NULL。界面会说「没有返回数据」，用户读成
        # 「我业务上没有这件事」——一句关于他自己业务的假话。与「数字算错了」
        # 是两种失败，分开计。
        return "empty"
    return "correct" if _matches_truth(rows, truth) else "wrong"


def _resolve_version(app, suite: dict[str, Any]) -> dict[str, Any]:
    """`etag` 与 `schema_hash` 现场从目录读，不写进题集。

    写死它们等于每次有人碰一下 Revision，整套基准就跑不了——实测 20 道全以
    `RevisionConflictError: revision etag changed` 报错、0ms、看起来像系统坏了。
    题集该描述"问什么、正确答案是什么"，版本是运行时事实。
    """

    if suite.get("entry") == "release":
        return suite
    revision = app.catalog.get_revision(suite["revision_id"])
    return {
        **suite,
        "etag": revision.etag,
        "schema_hash": revision.schema_snapshot_hash,
    }


def _run_once(app, suite: dict[str, Any], question: str) -> tuple[dict[str, Any], float]:
    """两种入口，按题集的 `entry` 选。

    - `revision`（默认）：候选 Revision 的发布前试问。
    - `release`：已发布 Release，**直接进 `_query_service`** 而不走
      `app.query`——后者每次都写一行诊断，而诊断表按 actor+project 封顶 100 行，
      跑批会把线上真实历史冲掉，那正是我们用来测量的东西。
      同一条链路，只是不写审计。
    """
    from knowflow_analytics.query.contracts import QueryRequest

    request = QueryRequest(
        project_id=suite["project_id"], question=question, include_diagnostics=True
    )
    started = perf_counter()
    if suite.get("entry") == "release":
        response = app._query_service.query(request, actor_id=suite["actor_id"])
    else:
        response = app.preview_revision_query(
            revision_id=suite["revision_id"],
            request=request,
            expected_etag=suite["etag"],
            expected_schema_snapshot_hash=suite["schema_hash"],
            actor_id=suite["actor_id"],
        )
    return response.model_dump(mode="json"), (perf_counter() - started) * 1000


def _run_case(app, suite: dict[str, Any], case: dict[str, Any], runs: int) -> dict[str, Any]:
    truth = None
    truth_note = ""
    if case.get("truth_sql"):
        try:
            truth = _truth_rows(app, suite["project_id"], case["truth_sql"])
        except Exception as exc:  # 真值拿不到就退成只看指纹，不能把它算成答错
            truth_note = f"真值失败: {type(exc).__name__}: {exc}"[:200]
    attempts = []
    for _ in range(runs):
        try:
            dump, elapsed = _run_once(app, suite, case["q"])
            attempts.append(
                {
                    "verdict": _classify(dump, truth),
                    "fingerprint": _fingerprint(dump),
                    "elapsed_ms": round(elapsed),
                }
            )
        except Exception as exc:
            attempts.append(
                {
                    "verdict": "error",
                    "fingerprint": {"state": "EXCEPTION", "code": type(exc).__name__},
                    "detail": str(exc)[:300],
                    "elapsed_ms": 0,
                }
            )
    verdicts = Counter(a["verdict"] for a in attempts)
    return {
        "id": case["id"],
        "question": case["q"],
        "expect": case.get("expect"),
        "note": case.get("note", ""),
        "truth_rows": None if truth is None else len(truth),
        "truth_note": truth_note,
        "verdicts": dict(verdicts),
        "stable": len(verdicts) == 1,
        "attempts": attempts,
    }


def _headline(results: list[dict[str, Any]]) -> dict[str, int]:
    """按每题的多数结局计一次。单次运行的波动不该被当成两道题。

    `expect == "fingerprint"` 的题不进正确率汇总：它们没有真值，0 行有可能正是
    正确答案（acct-04「是否借贷平衡」全平时就该 0 行），混进来会把对的记成假空。
    """
    tally: Counter[str] = Counter()
    for item in results:
        if item.get("expect") == "fingerprint":
            continue
        verdicts = item["verdicts"]
        tally[max(verdicts, key=lambda k: verdicts[k])] += 1
    return dict(tally)


def _print_report(payload: dict[str, Any]) -> None:
    results = payload["results"]
    rule = "=" * 78
    print(f"\n{rule}")
    print(f"题集 {payload['suite']} · 每题 {payload['runs']} 次 · {len(results)} 道")
    print(rule)
    for item in results:
        flag = " " if item["stable"] else "~"
        verdicts = ",".join(f"{k}×{v}" for k, v in sorted(item["verdicts"].items()))
        elapsed = [a["elapsed_ms"] for a in item["attempts"] if a["elapsed_ms"]]
        median = round(statistics.median(elapsed)) if elapsed else 0
        print(f"{flag}{item['id']:<12} {verdicts:<28} {median:>6}ms  {item['question'][:34]}")
        if item["truth_note"]:
            print(f"             ! {item['truth_note']}")
    scored = [item for item in results if item.get("expect") != "fingerprint"]
    fp_only = [item["id"] for item in results if item.get("expect") == "fingerprint"]
    print(f"\n汇总（{len(scored)} 道计分题，按每题多数结局）: {_headline(results)}")
    if fp_only:
        print(f"纯指纹题（不计分，只看变化）: {', '.join(fp_only)}")
    unstable = [item["id"] for item in results if not item["stable"]]
    if unstable:
        print(f"同题翻面（~）: {', '.join(unstable)}")


def _diff(base: dict[str, Any], after: dict[str, Any], noise: dict[str, Any] | None) -> None:
    by_id = {item["id"]: item for item in base["results"]}
    noisy = set()
    if noise is not None:
        noise_by_id = {item["id"]: item for item in noise["results"]}
        noisy = {
            key
            for key, item in by_id.items()
            if key in noise_by_id and item["verdicts"] != noise_by_id[key]["verdicts"]
        }
        print(f"噪声地板：基线自己跑两遍，{len(noisy)}/{len(by_id)} 道结局不同 → {sorted(noisy)}\n")
    # 拒答 > 静默错答/假空：拒答用户知道没拿到答案，后两者他以为拿到了。
    rank = {
        "correct": 5,
        "answered": 4,
        "clarify": 3,
        "refuse": 2,
        "empty": 1,
        "wrong": 0,
        "error": 0,
    }
    better, worse, drift = [], [], []
    for item in after["results"]:
        old = by_id.get(item["id"])
        if old is None:
            continue
        old_top = max(old["verdicts"], key=lambda k: old["verdicts"][k])
        new_top = max(item["verdicts"], key=lambda k: item["verdicts"][k])
        tag = " (噪声内)" if item["id"] in noisy else ""
        line = f"  {item['id']:<12} {old_top:>9} → {new_top:<9}{tag}  {item['question'][:34]}"
        if rank[new_top] > rank[old_top]:
            better.append(line)
        elif rank[new_top] < rank[old_top]:
            worse.append(line)
        elif _fingerprints_differ(old, item):
            drift.append(line.replace(f"{old_top:>9} → {new_top:<9}", f"{'指纹变了':>9}   "))
    print(f"改善 {len(better)}：\n" + ("\n".join(better) or "  —"))
    print(f"\n回归 {len(worse)}：\n" + ("\n".join(worse) or "  —"))
    print(
        f"\n结局没变但指纹变了 {len(drift)}（改了 SQL/作用域/列，需人看）：\n"
        + ("\n".join(drift) or "  —")
    )
    print(f"\n基线汇总: {_headline(base['results'])}\n之后汇总: {_headline(after['results'])}")


_ALIAS_RE = re.compile(r'\bAS\s+("(?:[^"]*)"|[^\s,)]+)', re.IGNORECASE)


def _shape(fingerprint: dict[str, Any]) -> str:
    """指纹的可比形态：别名归一，`DISTINCT` 忽略。

    实测同一份代码跑两遍，10 道题里 7 道指纹不同，其中 4 道只是别名在抖
    （`"差旅费"` vs `"差旅费_"`、`"_销售费用_"` vs `"销售费用"`、多写一个
    `DISTINCT`）——作用域、列、数字全同，是同一个答案。不归一的话这 4 条良性
    抖动会把真信号（作用域在两张表之间跳、超时、写法整个换掉）盖住。
    """
    comparable = dict(fingerprint)
    sql = comparable.get("s2sql")
    if isinstance(sql, str):
        comparable["s2sql"] = _ALIAS_RE.sub("AS _", sql.replace("SELECT DISTINCT", "SELECT"))
    return json.dumps(comparable, sort_keys=True, ensure_ascii=False)


def _fingerprints_differ(old: dict[str, Any], new: dict[str, Any]) -> bool:
    def shapes(item: dict[str, Any]) -> set[str]:
        return {_shape(a["fingerprint"]) for a in item["attempts"]}

    return shapes(old) != shapes(new)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", help="题集 JSON")
    parser.add_argument("--runs", type=int, default=3, help="每题跑几次（默认 3，翻面是常态）")
    parser.add_argument("--out", help="结果写到哪")
    parser.add_argument("--only", help="只跑 id 前缀匹配的题")
    parser.add_argument("--diff", nargs=2, metavar=("BASE", "AFTER"))
    parser.add_argument("--noise", help="基线的第二次运行，用来量噪声地板")
    args = parser.parse_args()

    if args.diff:
        base = json.loads(Path(args.diff[0]).read_text(encoding="utf-8"))
        after = json.loads(Path(args.diff[1]).read_text(encoding="utf-8"))
        noise = json.loads(Path(args.noise).read_text(encoding="utf-8")) if args.noise else None
        _diff(base, after, noise)
        return

    if not args.suite:
        parser.error("要么 --suite 跑一遍，要么 --diff 比两遍")
    suite = json.loads(Path(args.suite).read_text(encoding="utf-8"))
    cases = [c for c in suite["cases"] if not args.only or c["id"].startswith(args.only)]

    import knowflow_analytics.query.service as service_module

    print(f"跑的代码: {service_module.__file__}")
    app = _load_application()
    suite = _resolve_version(app, suite)
    if suite.get("entry") != "release":
        print(f"Revision {suite['revision_id']} etag={suite['etag']}")
    results = [_run_case(app, suite, case, args.runs) for case in cases]
    payload = {
        "suite": suite.get("name", args.suite),
        "runs": args.runs,
        "source": service_module.__file__,
        "results": results,
    }
    _print_report(payload)
    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写入 {args.out}")


if __name__ == "__main__":
    main()
