"""槽位抽取原型：一次 LLM 调用把问题填成槽位，走已有结构化路径。

    # 容器内跑（需要 app 与模型网关）
    docker cp scripts/query_slot_extraction_prototype.py c:/tmp/bench/extract.py
    docker exec c sh -c "cd /tmp/bench && python extract.py"

**2026-09-21 在冻结基线上实测 20 / 19 / 19 对，三遍零错答、零假空**
（两次 19 是模型网关超时，每次打中不同的题，属基础设施抖动；原型没做超时重试）。
同一 20 道题生产链路是 correct 13 / empty 4 / wrong 2 / clarify 1，
按「有数字真值」的 14 道算：生产 8 对，原型 14 对。

与现在的链路两点不同：
① 模型不写 SQL，填一组有类型的槽位——漏一个 filter 是结构可见的，
   写错字段名是枚举外的值、当场拒。
② 提示词里**没有任何相似性提示**（没有 mapped_constraints、没有"这个词像什么"）。
   只有目录与业务词典的声明。相似性检索是 IR，它回答"这个词像什么"；
   这里要的是"这句话在说什么"，那是语义解析。
"""

import json
import sys

sys.path.insert(0, "/tmp/bench")  # 与 query_replay_benchmark.py 同目录
from query_replay_benchmark import _blank, _load_application, _numbers
from sqlalchemy import text

from knowflow_analytics.contracts import (
    FilterOperator,
    QueryFilter,
    SemanticQuery,
    SemanticQueryType,
)
from knowflow_analytics.query.contracts import StructuredQueryRequest

_SUITE_PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/bench/query_suites/acct.json"
with open(_SUITE_PATH, encoding="utf-8") as _fh:
    SUITE = json.load(_fh)
PID = SUITE["project_id"]
LOW_CARD = 25  # 取值不超过这么多就整个内联——它顺带教会模型格式

app = _load_application()
svc = app._query_service
pub = svc._releases.get_active_release(PID)
r = pub.release
dims = {d.id: d for d in r.dimensions}
mets = {m.id: m for m in r.metrics}
fields = {f.id: f for f in r.fields}
ds_by_name = {d.name: d for d in r.datasets}

binding = app._sources.for_project(PID)
values_by_dim = {}
for v in r.dimension_values:
    values_by_dim.setdefault(v.dimension_id, []).append(v)


def _walk(lines, name_of, children, codes, *, depth):
    """把一层节点写进 lines，再递归它的子节点。

    提成顶层函数而不是闭包：闭包会捕获外层循环变量（ruff B023），
    多棵树时后一棵会改写前一棵看到的名字表。
    """

    for code in sorted(codes, key=lambda x: name_of.get(x, "")):
        lines.append(f"{'  ' * depth}{name_of[code]}")
        _walk(lines, name_of, children, children.get(code, []), depth=depth + 1)


def hierarchy_trees() -> dict[str, str]:
    """按声明的 `hierarchies` 把层级维度渲染成一棵树。

    **优先读 release 里的 `DimensionValueSpec.parent_value`**（2026-09-21 起由发布期
    写入），读不到才回落到按声明去客户库取一次父子配对——升级期两种 release 并存。

    不消费这条声明的代价实测得到：D008 问「应收账款」（父科目），凭证只记在
    `应收账款-甲公司` 上，按父科目精确筛返回 0 行，界面说"没有数据"。
    """
    published = _trees_from_release()
    if published:
        print(f"层级树来自 release（{len(published)} 个维度）")
        return published
    print("层级树：release 里没有 parent_value，回落读一次客户库")
    return _trees_from_database()


def _trees_from_release() -> dict[str, str]:
    by_dim: dict[str, dict[object, object]] = {}
    label: dict[str, str] = {}
    for v in r.dimension_values:
        if v.parent_value is None:
            continue
        by_dim.setdefault(v.dimension_id, {})[v.value] = v.parent_value
        label[v.dimension_id] = dims[v.dimension_id].name if v.dimension_id in dims else ""
    out = {}
    for dimension_id, pairs in by_dim.items():
        # 树里要有该维度的**全部已发布取值**，不只是有父子关系的那些。
        # 只放有父指针的会让「库存现金」这种没有上下级的科目整个消失，
        # 而模型要照这棵树挑名字。
        name_of = {v.value: str(v.value) for v in values_by_dim.get(dimension_id, [])}
        for value in pairs:
            name_of.setdefault(value, str(value))
        for parent in pairs.values():
            name_of.setdefault(parent, str(parent))
        children: dict[object, list[object]] = {}
        for child, parent in pairs.items():
            children.setdefault(parent, []).append(child)
        roots = [value for value in name_of if value not in pairs]
        lines: list[str] = []
        _walk(lines, name_of, children, roots, depth=0)
        if lines and label.get(dimension_id):
            out[label[dimension_id]] = "\n".join(lines)
    return out


def _trees_from_database() -> dict[str, str]:
    out = {}
    for h in r.hierarchies:
        levels = [dims.get(i) for i in h.levels]
        if len(levels) != 2 or any(x is None for x in levels):
            continue
        parent_dim, child_dim = levels
        pf, cf = fields.get(parent_dim.field_id), fields.get(child_dim.field_id)
        if pf is None or cf is None:
            continue
        model = next((m for m in r.models if m.id == h.model_id), None)
        if model is None:
            continue
        # 这个模型的"名称"维度：树用它显示，因为用户说的是名字不是编码
        label = next(
            (d for d in r.dimensions if d.model_id == h.model_id and d.name.endswith("名称")), None
        )
        if label is None:
            continue
        lf = fields.get(label.field_id)
        if lf is None:
            continue
        sql = f'SELECT "{pf.column}", "{cf.column}", "{lf.column}" FROM "{model.table}"'
        try:
            with binding.engine.connect() as conn:
                rows = conn.execute(text(sql)).fetchall()
        except Exception:
            continue
        name_of = {code: name for _, code, name in rows}
        children = {}
        for parent, code, _ in rows:
            if parent is not None and parent in name_of:
                children.setdefault(parent, []).append(code)
        roots = [c for p, c, _ in rows if p is None or p not in name_of]

        lines: list[str] = []
        _walk(lines, name_of, children, roots, depth=0)
        if any(children.values()):
            out[label.name] = "\n".join(lines)
    return out


HIER = None  # 渲染时填


def render_catalog() -> str:
    """按作用域给成员，并标出哪些属于事实根。

    两条都是实测逼出来的：
    ① 维度**不是**每个作用域都有——科目余额分析没有往来单位名称和部门名称。
       渲染成一份共享列表会让模型填出越界的槽位（D008/D009 因此被拒）。
    ② 同一作用域里可能有两个同义的期间维度：`所属期间` 是事实表自己的列，
       `会计期间` 是冻结路由从 dim_period 接来的。目录知道哪个属于事实根，
       标出来模型才分得清（D010/D011 因此答空）。
    """
    roots = {rt.dataset_id: rt.root_model_id for rt in r.analysis_topic_routes}
    out = ["## 分析范围（选一个）"]
    for d in r.datasets:
        root = roots.get(d.id)
        ms = [mets[i].name for i in d.metric_ids if i in mets and visible(i)]
        own, borrowed = [], []
        for i in d.dimension_ids:
            dm = dims.get(i)
            if dm is None:
                continue
            if visible(i):
                (own if dm.model_id == root else borrowed).append(dm.name)
        out.append(f"\n### {d.name}")
        out.append(f"  指标: {', '.join(ms)}")
        out.append(f"  本表自己的维度（优先用这些）: {', '.join(sorted(set(own)))}")
        out.append(f"  可关联的维度: {', '.join(sorted(set(borrowed)))}")

    out.append("\n## 维度取值与类型（上面出现的名字，格式照抄）")
    named = {}
    for d in r.datasets:
        for i in d.dimension_ids:
            dm = dims.get(i)
            if dm is not None and visible(i):
                named.setdefault(dm.name, []).append(i)
    for name, ids in sorted(named.items()):
        f = fields.get(dims[ids[0]].field_id)
        vs = [v for i in ids for v in values_by_dim.get(i, [])]
        # **给业务名，不是给原始值。** 原始值可能是编码（账款方向 1/2），
        # 业务含义在 display_name / 别名里。只给原始值等于把编码摆给模型看，
        # 它只能去找别的、字面像的维度（实测 D008：用 科目名称='应收账款'）。
        labels = set()
        for v in vs:
            biz = (v.display_name or "").strip()
            extra = "/".join(v.aliases) if v.aliases else ""
            if biz and biz != str(v.value):
                labels.add(f"{v.value!r}={biz}" + (f"（{extra}）" if extra else ""))
            else:
                labels.add(repr(v.value))
        uniq = sorted(labels)
        al = f" 别名={list(dims[ids[0]].aliases)}" if dims[ids[0]].aliases else ""
        if uniq and len(uniq) <= LOW_CARD:
            out.append(f"- {name}（{f.data_type if f else '?'}）取值: {', '.join(uniq)}{al}")
        else:
            hint = f"，约 {len(uniq)} 个取值" if uniq else ""
            out.append(f"- {name}（{f.data_type if f else '?'}{hint}）{al}")

    for dim_name, tree in (HIER or {}).items():
        out.append(f"\n## {dim_name} 是一棵树（上级在上、下级缩进）")
        out.append("问上级时要把它和它下面所有下级一起算——用 op=in 把它们全列出来，")
        out.append("或者 op=contains（下级名字都以上级名字开头）。只用 op=eq 只会匹配上级那一条，")
        out.append("而明细通常只记在末级上。")
        out.append("```")
        out.append(tree)
        out.append("```")

    if r.terms:
        out.append("\n## 业务词典（建模者声明：用户这么说时，指的是这些成员）")
        for t in r.terms:
            tgt = [mets[i].name for i in t.metric_ids if i in mets and visible(i)] + [
                dims[i].name for i in t.dimension_ids if i in dims and visible(i)
            ]
            if not tgt:
                continue
            al = f"（也说 {', '.join(t.aliases)}）" if t.aliases else ""
            out.append(f"- 「{t.name}」{al} → {', '.join(tgt)}")
    return "\n".join(out)


RULES = """你把用户的问题填成槽位。**不要写 SQL。**

输出 JSON：
{"scope": "...", "metric": "...",
 "filters": [{"dimension": "...", "op": "eq|contains|between|in", "value": ...}],
 "group_by": [...], "unsupported": null}

- scope / metric / dimension 只能用目录里**逐字出现过的名字**，不许改写、不许自创。
- dimension 必须是所选 scope 名下列出的那些；同义时优先「本表自己的维度」。
- op=contains 表示子串匹配（用户说了个简称、或想要一类科目时用它）；value 不带 %。
- op=between 的 value 是两个元素的数组；op=in 的 value 是数组。
- 维度列了取值的，value 必须是其中之一，格式照抄（例如会计期间是 '2024-01' 这种）。
- 问题问"多少/金额"选金额类指标，问"几张/几笔/多少个"选数量类指标。
- 业务词典说了某个说法指哪些成员，就按它填——那是建模者明确声明的。
- 只按问题**明说的**条件填 filters，不要自己加时间或其它限制。
- shape=detail 表示列明细行（"列出/明细/清单/有哪些"），此时 metric 可以不填；
  shape=aggregate（默认）表示算合计。
- 需要占比、环比、同比、排名、字符串拼接、或其它槽位表达不了的形态时，
  把 unsupported 填成一句话说明，其余留空。"""

# 行列级权限：None 表示不收窄。**两处都要过**——目录渲染时不能让模型看见，
# 组装层还要再判一次。只做前者是"前端显隐当安全边界"，只做后者是把不该看的
# 名字摆给模型（它会照着填，然后被拒，用户看到一句莫名其妙的拒答）。
VISIBLE: frozenset[str] | None = None


def visible(element_id: str) -> bool:
    return VISIBLE is None or element_id in VISIBLE


HIER = hierarchy_trees()
print(f"层级树: {list(HIER)}")
CATALOG = render_catalog()
print(f"目录 {len(CATALOG)} 字，规则 {len(RULES)} 字")

SLOT_SCHEMA = {
    "type": "object",
    "properties": {
        # 允许 null：模型判断"槽位表达不了"时，scope/metric 本来就没有值。
        # 标成 required string 会让它**正确的拒答被网关拒掉**——实测预警题集里
        # 5 道（逗号拼接字符串聚合、维度不存在、以及一句根本不是查询的话）
        # 都是这么丢掉的，看起来像模型出错，其实是契约不允许它说"不行"。
        "scope": {"type": ["string", "null"]},
        "metric": {"type": ["string", "null"]},
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string"},
                    "op": {"type": "string", "enum": ["eq", "contains", "between", "in"]},
                    "value": {},
                },
                "required": ["dimension", "op", "value"],
            },
        },
        "group_by": {"type": "array", "items": {"type": "string"}},
        "shape": {"type": ["string", "null"], "enum": ["aggregate", "detail", None]},
        "unsupported": {"type": ["string", "null"]},
    },
    # 一个 required 都不留：模型说"表达不了"时任何字段都可能没有值，
    # 把它们标成必填会让**正确的拒答被网关拒掉**（实测预警题集丢了 5~7 道）。
    # 合法性由组装层判，不由 JSON schema 判。
    "required": [],
}

OPS = {"eq": FilterOperator.EQ, "between": FilterOperator.BETWEEN, "in": FilterOperator.IN}
gateway = svc._orchestrator._llm_parser._gateway


def extract(question: str, rejection: str | None = None) -> dict:
    user = f"{CATALOG}\n\n## 问题\n{question}"
    if rejection:
        user += f"\n\n## 上一次的槽位被拒了\n{rejection}\n请改正后重填。"
    out = gateway.generate_json(
        purpose="analytics.s2sql",
        messages=[{"role": "system", "content": RULES}, {"role": "user", "content": user}],
        response_schema=SLOT_SCHEMA,
        trace={"tenant_id": SUITE["actor_id"], "attempt": "1", "purpose": "slot_extraction"},
    )
    return out if isinstance(out, dict) else json.loads(out)


class SlotRejected(Exception):
    """槽位里出现了枚举外的名字。这是新架构的核心性质：**当场拒，不猜**。"""


def to_query(slots: dict) -> SemanticQuery:
    scope = slots.get("scope")
    if scope not in ds_by_name:
        raise SlotRejected(f"分析范围不存在: {scope!r}")
    d = ds_by_name[scope]

    def metric_id(name):
        for i in d.metric_ids:
            if i in mets and mets[i].name == name:
                if not visible(i):
                    raise SlotRejected(f"没有权限访问指标 {name!r}")
                return i
        raise SlotRejected(f"「{d.name}」里没有指标 {name!r}")

    def dim_id(name):
        for i in d.dimension_ids:
            if i in dims and dims[i].name == name:
                if not visible(i):
                    raise SlotRejected(f"没有权限访问维度 {name!r}")
                return i
        raise SlotRejected(f"「{d.name}」里没有维度 {name!r}")

    fs = []
    for f in slots.get("filters") or []:
        did = dim_id(f["dimension"])
        op, val = f.get("op", "eq"), f.get("value")
        # **eq 的值必须在已发布取值里。** 不在就拒，不静默执行——一个不存在的
        # 取值一定返回 0 行，而界面会把它渲染成"没有数据"，是一句关于用户业务的
        # 假话。实测 D008：模型写 科目名称 eq '应收账款'，而真实取值是
        # '应收账款-甲公司'，本该用 contains。拒了之后带原因重试就能改对。
        if op == "eq":
            known = {str(v.value) for v in values_by_dim.get(did, [])}
            if known and str(val) not in known:
                raise SlotRejected(
                    f"「{f['dimension']}」没有取值 {val!r}。"
                    f"它的取值是: {', '.join(sorted(known)[:8])}。"
                    f"想要包含这个词的那一类，用 op=contains。"
                )
        if op == "contains":
            fs.append(QueryFilter(dimension_id=did, operator=FilterOperator.LIKE, value=f"%{val}%"))
        elif op in OPS:
            fs.append(QueryFilter(dimension_id=did, operator=OPS[op], value=val))
        else:
            raise SlotRejected(f"不支持的算子 {op!r}")
    detail = (slots.get("shape") or "aggregate") == "detail"
    metric = slots.get("metric")
    return SemanticQuery(
        dataset_id=d.id,
        query_type=SemanticQueryType.DETAIL if detail else SemanticQueryType.AGGREGATE,
        metric_ids=(metric_id(metric),) if metric else (),
        dimension_ids=tuple(dim_id(n) for n in (slots.get("group_by") or [])),
        filters=tuple(fs),
    )


def run_suite():
    ok = bad = unsup = exc = rej = slotted = 0
    for case in SUITE["cases"]:
        cid, q = case["id"], case["q"]
        try:
            slots = extract(q)
            if slots.get("unsupported") or not slots.get("scope"):
                reason = slots.get("unsupported") or "没有给出分析范围"
                print(f"  {cid}  文本路（槽位表达不了）: {reason[:70]}")
                unsup += 1
                continue
            try:
                to_query(slots)
            except SlotRejected as first:
                print(f"  {cid}  （第一次被拒：{first}）")
                slots = extract(q, rejection=str(first))
            if slots.get("unsupported"):
                print(f"  {cid}  文本路（槽位表达不了）: {slots['unsupported'][:70]}")
                unsup += 1
                continue
            query = to_query(slots)
            slotted += 1
            if not case.get("truth_sql"):
                # 无真值题集：只量分流，答案对不对量不了
                print(f"  {cid}  槽位路 ✓  {json.dumps(slots, ensure_ascii=False)[:120]}")
                continue
            resp = svc.query_structured(
                StructuredQueryRequest(
                    project_id=PID,
                    semantic_query=query,
                    allowed_element_ids=None if VISIBLE is None else tuple(sorted(VISIBLE)),
                ),
                actor_id=SUITE["actor_id"],
            )
            rows = tuple(
                tuple(x)
                for x in ((resp.model_dump(mode="json").get("data") or {}).get("rows") or [])
            )
            with binding.engine.connect() as c:
                truth = tuple(tuple(x) for x in c.execute(text(case["truth_sql"])).fetchall())
            if _blank(rows) and _blank(truth):
                v, extra = "correct(都空)", ""
            elif _blank(rows) != _blank(truth):
                v, extra = "**空/非空不一致**", f"答={_numbers(rows)} 真={_numbers(truth)}"
            else:
                g, w = sorted(_numbers(rows)), sorted(_numbers(truth))
                same = len(g) == len(w) and all(
                    abs(a - b) <= 0.01 for a, b in zip(g, w, strict=True)
                )
                v, extra = ("correct", "") if same else ("**wrong**", f"答={g} 真={w}")
            ok += v.startswith("correct")
            bad += not v.startswith("correct")
            print(f"  {cid}  {v:<18} {extra}")
            if not v.startswith("correct"):
                print(f"        槽位={json.dumps(slots, ensure_ascii=False)[:190]}")
        except SlotRejected as e:
            rej += 1
            print(f"  {cid}  refuse(槽位越界)   {e}")
        except Exception as e:
            exc += 1
            print(f"  {cid}  **EXC** {type(e).__name__}: {str(e)[:110]}")

    total = len(SUITE["cases"])
    print(f"\n分流: 槽位路 {slotted}/{total} · 文本路 {unsup} · 拒 {rej} · 异常 {exc}")
    if ok or bad:
        print(f"槽位路里带真值的: 对 {ok} / 错 {bad}")


if __name__ == "__main__":
    run_suite()
