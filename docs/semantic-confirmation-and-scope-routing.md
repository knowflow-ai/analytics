# 语义确认与事实范围路由方案

| 项目 | 内容 |
| --- | --- |
| 文档版本 | 1.0 |
| 评审日期 | 2026-08-28 |
| V1 状态 | 已评审，本轮实施与验证 |
| V2 状态 | 2026-08-29 方案 A 已批准实施；自动采用仍按 shadow 门禁分场景放量 |
| 适用路径 | 自然语言问数（Natural-language question） |
| 决策阶段 | `CANDIDATE_DISCOVERY` |

## 1. 决策摘要

本方案在“系统自动推导”和“人工确认”之间采用以下边界：

1. 普通用户不感知也不选择 QueryScope。Scope 是发布版本内由编译器生成的内部执行边界，只能出现在高级诊断与审计 Trace 中。
2. 受治理指标的 direct exact 证据，或用户对 Mapper 候选的明确确认，可以锁定指标 owner（事实根）A。
3. Keyword、Term remap 与 Embedding 都是 weak evidence。它们只能召回业务指标候选，不能按最高分、分差、出现顺序或“只有一个候选”自动锁定 owner。
4. 系统只有在现有确定性证据不能唯一确定业务语义时才请求人工确认。确认项展示指标或业务对象，不展示 Scope、Dataset 名称、内部 ID 或路由路径。
5. 指标锁定 owner A 后，所有 direct exact 维度和维度值仍是硬约束。A 内不可达时返回 `DIMENSION_NOT_REACHABLE`，不得丢弃维度，也不得切换到事实根 B。
6. 本次只改变事实边界准入与确认方式，不改变自然语言路径的 textual S2SQL 解析、纠正、翻译顺序和权威性。

## 2. 目标与非目标

### 2.1 V1 目标

- 让系统尽可能依据受治理证据自动确定事实根。
- 当 weak metric 是确定事实根所缺的最后一项业务语义时，向用户确认指标，而不是让用户选择 Scope。
- 没有指标锚点且多个事实根仍然可行时，以业务对象名称和说明确认分析对象。
- 让确认后的指标成为独立的 `confirmed metric` owner 信号，同时保留原始 Mapper 方法和分数。
- 保证 exact 维度/维度值对 owner 路由拥有否决权。
- 保持所有候选、确认和拒绝决定可审计、可按 Release 版本复现。

### 2.2 V1 非目标

- 不让 LLM 选择 QueryScope、事实根、Join 路径或内部语义 ID。
- 不根据 embedding 分数或分差自动确认指标。
- 不在一次确认后自动写入、发布别名或 Term。
- 不增加跨事实根查询能力。
- 不改变结构化 Playground 管线。
- 不改变 LLMSqlParser / RuleSqlParser 的既有顺序、fallback 行为或 textual S2SQL 权威。

## 3. 术语与证据权力

| 术语 | 定义 | 决策权力 |
| --- | --- | --- |
| Scope / QueryScope | Release 内编译冻结的单事实根、成员与路径集合 | 仅内部规划、校验和高级诊断；普通用户不可见 |
| owner / 事实根 A | 指标所属的受治理 Model | direct exact 或 confirmed metric 可锁定 |
| direct exact metric | Mapper 对已发布指标规范名或受治理别名产生的直接 `EXACT`；不包括 Term remap、manifest 和 all-field | 可参与自动锁定 owner |
| confirmed metric | 用户从本次 Mapper 实际召回且仍属于当前 Release 的指标候选中明确选择的指标 | 可锁定 owner，但不能改写成 exact |
| weak metric | Keyword、Term remap 或 Embedding 召回的指标 | 只可进入确认候选，不能自动锁 owner |
| exact dimension | direct exact 维度，或 direct exact 维度值所归属的维度 | 硬约束；可否决已锁定 owner 的可执行性 |
| business object | 面向用户的事实模型业务名称与说明 | 没有指标锚点且仍有多个事实边界时用于确认 |

`exact_metric_ids`、`confirmed_metric_ids` 和原始 Mapping provenance 必须分别记录。用户确认只增加 `confirmed_metric_ids`，不得把 `method=embedding|keyword|term` 篡改为 `EXACT`。

## 4. V1 总体流程

```text
用户自然语言问题
        |
        v
PRECHECK：绑定 actor / Release / spec hash / index snapshot
        |
        v
CANDIDATE_DISCOVERY：跨全部已发布 Scope 全局检索一次
        |
        +-- direct exact：Metric / Dimension / DimensionValue
        +-- weak：Keyword / Term / Embedding candidate
        |             embedding 准入：relevance >= 0.90
        v
事实根决策（只消费 Mapper 证据与已发布 Scope 投影）
        |
        +-- 唯一 direct exact metric owner A --------+
        |                                             |
        +-- weak metric 候选存在                      |
        |      |                                      |
        |      +-- 展示全部存活的业务指标候选          |
        |      +-- 用户确认 metric M                  |
        |      +-- 记录 confirmed M -> owner A -------+
        |                                             |
        +-- 无 metric，系统可唯一收敛 ----------------+
        |                                             |
        +-- 无 metric，多个业务事实边界                |
        |      +-- 展示“业务对象”确认                  |
        |      +-- 不展示 Scope / Dataset / 路径 ------+
        |                                             |
        v                                             |
owner A 内校验 <--------------------------------------+
        |
        +-- 每个 exact dimension/value 均可达？-- 否 --> DIMENSION_NOT_REACHABLE
        |
        +-- 指标成员、冻结 Route、无扇出且版本一致？
        |       +-- 否 --> 稳定拒绝或业务语义澄清
        |       +-- 是 --> 内部选出唯一可执行 Scope
        v
选中 A 的完整受治理成员投影
        |
        v
既有自然语言 textual S2SQL 管线（顺序与权威不变）
        |
        v
参数化物理 SQL -> Guard -> Executor
```

Scope 在上述流程中始终存在，但只是系统的编译产物。普通确认卡片只表达“你指哪个指标”或“你要分析什么业务对象”。续跑使用短期、服务端签名的 `sel1` HMAC opaque continuation token；UI 不渲染内部 Scope ID。

每个 token 绑定 project、actor、原始问题、原始 dataset_ids 集、Release ID、spec hash、index snapshot、该张卡实际代表的 Scope/semantic 组合与过期时间。主服务和 OSS runtime 都用各自已有的 service secret 作为签名密钥来源。服务端先 O(1) 验证 HMAC、版本、上下文和 TTL；只有合法 token 才扫描当前 Release 恢复内部对象，避免无效输入放大全目录遍历。由于 token 只能由服务端为实际展示的卡签发，未展示的同 root 内部 Scope、公开字段重算的旧 hash、任意拼接或篡改都不能绕过空选项 fail-close。

若一次问题依次需要“确认语义元素”和“确认业务对象”，或反向先确认业务对象再确认语义元素，同一个 opaque token 同时携带两次选择。wire 不包含 Dataset ID 或原始 semantic ID；客户端不解析 token，只从原 QueryTurn 保存的实际 QueryInput 续跑，保留 dataset_ids、conversation_id 等上下文并刷新版本三元组。跨问题、跨 actor、跨 dataset 集、旧版本、过期、非唯一恢复或非法组合全部 fail closed；不得丢失第一步选择后重新进入相同澄清循环。

## 5. 自动推导与人工确认的决策表

| 已有证据 | 系统动作 | 是否询问用户 | 说明 |
| --- | --- | --- | --- |
| 唯一 direct exact 指标，且 owner A 内所有 exact 维度可达 | 自动锁 owner A，并选择唯一安全内部 Scope | 否 | direct exact 是自动路由硬证据 |
| 同一 exact 说法命中多个跨事实根指标 | 展示这些业务指标 | 是，确认指标口径 | 不展示其 Scope；不按顺序或分数选第一个 |
| 多个独立 exact 指标分别属于不同事实根 | `CROSS_FACT_METRICS_UNSUPPORTED` | 否，提示拆分问题或建联合事实模型 | 单次选择不能安全抹掉另一个明确指标 |
| 没有 exact 指标，恰好一个 weak phrase 且候选全为 Metric | 先运行受限 AI 业务裁决；唯一 MATCH 且治理校验通过则继续 | AI 弃权、失败或治理不唯一时确认 | embedding/keyword 分数本身不能把 weak 升级成事实 |
| AI 唯一裁决 weak metric M | 记录 `ai_adjudicated_metric_ids=[M]`，再由 Resolver 锁 owner | 否，治理通过后继续 | 不伪装成 exact 或人工确认；维度仍有否决权 |
| 用户确认 weak metric M | 记录 `confirmed_metric_ids=[M]`，锁定 M 的 owner A | 否，继续执行 | 原方法和分数保留；确认不等于 exact |
| 指标 owner A 已锁定，exact 维度只在事实根 B 可达 | `DIMENSION_NOT_REACHABLE` | 否，明确指出不可组合 | 不忽略维度，不切换到 B |
| 无 metric，exact 维度等证据可唯一收敛到一个业务事实边界 | 自动进入该内部边界 | 否 | 系统已经能确定时不打扰用户 |
| 无 metric，多个事实边界仍可行 | 展示事实根 Model 的业务名称与说明 | 是，确认业务对象 | 选项类型为 `analysis_object`，文案不出现 Scope |
| owner A 内仍有无法由既有合同解决的同名语义歧义 | 沿用既有语义澄清 | 是，确认指标/维度 | 不把内部 Scope 选择伪装成语义选择 |
| Release、spec 或 index 已变化，旧确认 token 失效 | fail closed，要求从当前版本重新确认 | 是 | 防止确认与实际执行对象漂移 |

### 5.1 weak metric 候选生成

候选生成仍由现有 Mapper 投影负责，不新增在线语义启发式：

1. 全局 Evidence 只检索一次，再投影到可行 Scope。
2. 先使用既有 Moderate 投影中的 Keyword / Term 指标候选。
3. 没有上述候选时，再使用既有 Moderate、随后 Loose 投影中的 Embedding 指标候选。
4. 对同一 detected phrase 按 semantic ID 去重；展示该证据类别下全部存活候选。
5. 不比较候选分差，不以最高分或返回顺序选中任何候选。
6. 一次 continuation token 只确认一个 detected phrase；多个独立 weak metric phrase 不能被一次选择静默合并。

### 5.2 AI weak-metric 裁决（2026-08-28 已评审）

AI 裁决只替代一部分人工确认，不替代 Mapper、QueryScopeResolver 或 textual S2SQL Parser。它是 `CANDIDATE_DISCOVERY` 内的子步骤，不增加新的顶层 QueryStage：

```text
全局 Mapper evidence
  -> 现有 weak metric 单短语候选集
  -> AI semantic adjudicator
       +-- MATCH(Cn) ------> 验证 Cn 属于本轮候选
       |                     -> QueryScopeResolver 重新解析
       |                     -> 唯一、安全 Scope 才继续
       +-- AMBIGUOUS ------> 原人工确认卡
       +-- NONE -----------> 原人工确认卡
       +-- unavailable ----> 原人工确认卡
```

调用 AI 必须同时满足：

1. 没有 direct exact metric；
2. 没有用户 continuation 或已选业务对象；
3. 现有 Resolver 尚未稳定拒绝；
4. 恰好一个 weak detected phrase；
5. 候选非空、未溢出且全部为 Metric。

多个独立 weak phrase、metric/dimension 跨类型候选、候选溢出、exact metric 歧义和既有跨事实根多指标查询均不调用该裁决器。

模型输入只允许：

- 原始自然语言问题与 detected phrase；
- 局部候选键 `C1..Cn`；
- 指标业务名称、别名、定义、聚合和单位；
- 所属实体的业务名称与说明；
- 已由 Mapper 确认的维度/维度值业务文本。

模型输入禁止出现 semantic ID、Dataset/QueryScope 名称或 ID、root model ID、物理 schema/table/column、Join 路径、HMAC token、S2SQL、embedding score 或 score gap。Catalog 文本按不可信数据编码，不能作为 Prompt 指令执行。

候选数、每个资源的别名数/长度和整包用户 Prompt 均有固定上限；超过上限在调用模型网关前按 unavailable 处理并回退人工，不能把任意大 Catalog 文本发送给外部供应商。

模型输出固定为：

```json
{
  "decision": "MATCH | AMBIGUOUS | NONE",
  "candidate_key": "C1 或 null",
  "reason": "简短业务理由"
}
```

`MATCH` 必须且只能带一个当前候选键；其它决策不得带键。模型不得返回内部 ID，也不采信其自报 confidence。未知键、非法结构、超时、网关异常或不可区分候选视为 unavailable。

成功的 AI 选择以 `ai_adjudicated_metric_ids` 独立记录；`exact_metric_ids` 和人工 `confirmed_metric_ids` 保持不变，原 Mapper method/score 也不能改写。事实根路由只把三者做所有权并集：

```text
routing metrics = exact + human confirmed + AI adjudicated
```

AI 选择仍受 exact dimension/value、冻结 Route、成员、扇出与版本门禁约束。若 Resolver 无法得到唯一、安全 Scope，系统不得尝试第二名、按分数改选或切换事实根，而是回退原人工指标确认。成功选出 Scope 后，继续进入第 8 节完全不变的 textual S2SQL 管线。

主服务与 OSS 的安全默认是 `shadow`：运行裁决并记录结果，但仍返回现有人工确认卡。只有完成真实产品影子校准后显式配置 `auto` 才允许采用 MATCH；`off` 完全不调用。无模型或模型不可用时，行为必须退化为现有 Metric-first V1 人工确认基线。

## 6. Embedding `0.9` 与人工确认的关系

Embedding 阈值是召回准入阈值，不是正确率、概率或自动执行阈值。

```text
relevance = (cosine + 1) / 2

relevance >= 0.90
<=> cosine >= 0.80
```

因此，默认 `embedding_similarity=0.90` 应比较上游口径的 relevance，而不是直接与原始 cosine 比较。Mapping Trace 可保留原始 cosine 和方法，但展示或告警必须明确分数字段的口径。

```text
embedding relevance < 0.90
    -> 不进入 embedding weak candidate 集

embedding relevance >= 0.90
    -> 可以进入 weak metric candidate 集
    -> 分数本身仍不能锁定 owner
    -> 可进入第 5.2 节 AI 业务语义裁决
    -> AI 弃权/不可用时由用户确认
```

即使 relevance 为 0.99、候选只有一个，分数本身仍然只表示“系统找到了一个值得裁决的候选”。只有独立 AI 业务裁决并通过确定性治理，或用户确认后，才能分别成为 `ai_adjudicated_metric_ids` 或 `confirmed_metric_ids`。阈值影响召回率和裁决噪声，不改变语义证据的权力等级。

## 7. exact 维度否决合同

锁定事实根的目的是缩小执行边界，不是让指标覆盖用户的其它明确语义。对 owner A 进行 Scope 收敛时，必须同时检查每个 exact dimension phrase group：

```text
confirmed/exact metric M -> owner A
exact dimensions D = {D1, D2, ...}

feasible(A) = published scopes rooted at A
              ∩ scopes containing M
              ∩ scopes whose frozen route reaches every exact D

feasible(A) = empty and A has a published metric scope
    -> DIMENSION_NOT_REACHABLE
```

示例：

```text
“销售额” -> embedding 候选“订单净金额” -> AI 裁决或用户确认 -> owner=订单
“商品类别” -> exact dimension，但订单事实根冻结 Route 不可达

结果：DIMENSION_NOT_REACHABLE
禁止：忽略“商品类别”后查询订单
禁止：偷偷切换到商品明细事实根
```

## 8. textual S2SQL 管线保持不变

V1 只在 `CANDIDATE_DISCOVERY` 内确定事实边界和必要的 HITL continuation。选择 owner / Scope 后，继续使用选中 Scope 的 Mapper 投影和完整成员进入既有自然语言管线：

```text
NL2SQLParser
  -> Mapper + selected SemanticParseInfo
  -> LLMSqlParser / RuleSqlParser（既有固定顺序与 fallback）
  -> textual parsedS2SQL
  -> QueryTypeParser + registered Correctors
  -> textual correctedS2SQL（继续保持权威）
  -> SqlQueryParser
  -> DimExpressionParser / MetricExpressionParser / MetricRatioParser
  -> OntologyQueryParser
  -> semantic Translator
```

路由器不得读取问题关键词补造语义，不得生成 S2SQL，不得要求 LLM 输出内部 metadata ID，也不得改变选中候选的 textual S2SQL。结构化 Query / Playground 继续走 `QueryStructReq -> StructQueryParser -> semantic Translator`，不与自然语言路径合并。

## 9. 普通用户与高级诊断的边界

| 表面 | 可以展示 | 不可以展示 |
| --- | --- | --- |
| 普通问数与确认卡片 | 指标名、指标说明、业务对象名、业务对象说明、不可解析的 continuation token | `QueryScope`、`Scope`、Dataset 内部名/ID、root model ID、Route、Join 路径 |
| 普通错误提示 | “指标与维度不可组合”“请选择业务指标/业务对象” | 要求用户理解或配置 Scope 的修复步骤 |
| 高级诊断 / Trace | Release、index、原始 Mapper provenance、exact/confirmed IDs、内部 Scope 决策、冻结 Route | 用诊断数据反向参与在线语义选择 |
| 建模诊断 | Scope 覆盖率、路径、扇出、命名冲突 | 把 QueryScope 变成普通用户必须编辑的资源 |

普通 clarification wire 不序列化 option 的 `dataset_id`。内部 Dataset 选择只存在于服务端解析后的执行状态和显式开启的高级诊断中。

## 10. V1 风险与护栏

| 风险 | 可能后果 | V1 护栏 |
| --- | --- | --- |
| 把高 embedding 分数当成自动正确 | 静默路由到错误事实根 | 0.90 只准入候选；必须再经过 AI 业务裁决和确定性治理，失败则人工确认 |
| 用户确认被伪装成 exact | 审计无法区分词典事实和本次选择 | 分开记录 `exact_metric_ids` 与 `confirmed_metric_ids`，保留原始方法和分数 |
| metric-first 丢失不兼容维度 | 返回语法正确但业务错误的数字 | 收集全局 exact 维度后再收敛；不可达稳定拒绝 |
| 多候选 first-wins | 结果依赖索引顺序或分数抖动 | 全量展示存活候选；排序仅展示，不产生选择 |
| 把 Scope 暴露给用户 | 用户被迫理解内部执行模型，且发布变更破坏交互 | 普通 UI 仅用 metric / analysis object；签名 opaque token 不含可见内部 ID |
| 同一 root 下多个 Scope 显示成重复业务对象 | 人工看似有选择、实际无法区分 | 先由成员与冻结 Route 自动收敛；无法形成不同业务说明时 fail closed，并在建模诊断修复 |
| 确认后 Release 或索引漂移 | 用户确认的对象与实际执行对象不同 | HMAC continuation 绑定 release ID、spec hash、index snapshot ID 与 TTL |
| 客户端重算或重放未展示候选 | 绕过同 root 多 Scope 的空选项 fail-close，污染治理证据 | service-secret HMAC 绑定实际候选与 project/actor/question/dataset set；先验签后解析；旧 hash 和 cross-context replay 拒绝 |
| 两步确认只保留最后一个选择 | element/value 与业务对象之间循环，始终无法执行 | 同一签名 opaque token 双向绑定两次选择；QueryTurn 保留原始 QueryInput |
| 阈值过高造成漏召回 | 频繁退化到业务对象确认或无法理解指标 | 保持 fail closed；记录漏召回，V2 仅离线校准 |
| 阈值过低造成候选噪声 | AI 弃权与人工确认频率增加 | 分数不直接选择；监控候选数、AI 弃权率与人工放弃率，V2 离线校准 |
| 确认结果立即自动学习 | 错误或偶然选择污染长期词典 | V1 不记忆、不自动写 alias/Term |

## 11. 测试与验收指标

### 11.1 必须通过的合同测试

| 不变量 | 验收方式 | 目标 |
| --- | --- | --- |
| direct exact metric 唯一 owner 自动路由 | `test_scope_resolver.py`、`test_global_scope_routing.py` | 合同用例 100% 通过 |
| weak metric 不自动锁 owner | 单候选、多候选、分数与证据顺序变形测试 | 任何未确认 weak 用例执行调用数为 0 |
| confirmed metric 可锁 owner 但不成为 exact | 校验 owner、`confirmed_metric_ids`、`exact_metric_ids` 与 Mapper provenance | 合同用例 100% 通过 |
| exact dimension veto | confirmed/exact metric 与跨根 exact dimension 组合测试 | 100% 返回 `DIMENSION_NOT_REACHABLE`，执行调用数为 0 |
| Embedding 阈值口径 | `test_relevance_score_parity.py` | cosine 0.80 放行、0.79 拒绝 |
| 候选顺序/分数不改变 confirmed owner | 重排 evidence，并将 score 从低值改到高值 | 解析结果完全相同 |
| Scope 不进入普通确认文案 | 响应与 UI 渲染测试扫描 Scope 名称、内部 ID 和技术术语 | 普通表面泄漏数为 0 |
| 无 metric 时使用业务对象 | 多事实根 clarification 测试 | option kind 全部为 `analysis_object` |
| textual S2SQL 权威链不变 | `test_query_stage_pipeline.py` 及现有 parser/corrector/translator 回归 | 原有合同测试无回归 |
| 跨事实根明确指标不被确认覆盖 | 多独立 exact metric 测试 | 稳定返回 `CROSS_FACT_METRICS_UNSUPPORTED` |

除原始案例外，应至少覆盖以下 metamorphic 变形：语义 ID/业务名重命名、候选顺序颠倒、embedding 分数变化、添加无关 Scope、Release 续跑 token 过期、exact 维度顺序变化。实现不得依赖数据库名、表名、字段名、指标名或 benchmark wording。

### 11.2 上线观测指标

以下指标用于评估“自动推导与人工确认的平衡”，不作为在线自动猜测的新输入：

| 指标 | 定义 | V1 观察目的 |
| --- | --- | --- |
| 自动事实根确定率 | 无人工确认即确定 owner/唯一业务边界的请求数 ÷ 可路由请求数 | 判断 exact 与治理词典覆盖是否足够 |
| weak metric 确认率 | 进入 metric clarification 的请求数 ÷ 自然语言请求数 | 衡量用户摩擦 |
| 确认完成率 | 用户完成指标/业务对象选择的请求数 ÷ clarification 数 | 判断文案和候选质量 |
| 单次确认成功率 | 确认后首次续跑完成且未再次语义澄清的请求数 ÷ 已确认请求数 | 发现候选或 owner 内语义缺口 |
| 平均/P95 候选数 | 每次 metric clarification 的选项数 | 判断 embedding/Term 噪声 |
| embedding 边界分布 | 候选在 relevance 0.90 附近的分布及人工选择率 | 为 V2 离线校准提供数据 |
| `DIMENSION_NOT_REACHABLE` 率 | 该拒绝数 ÷ 已锁 owner 请求数 | 发现建模路径缺口或用户跨事实表达 |
| 普通表面 Scope 泄漏率 | 含内部 Scope 术语、名称或 ID 的普通响应数 ÷ 普通响应数 | 目标始终为 0 |
| 人工抽检误路由率 | 抽检中 owner 与裁决事实根不一致的自动完成请求数 ÷ 抽检自动完成数 | 必须作为准确性红线独立报告 |

产品准确性结论只能来自规定的端到端 product accuracy campaign；组件测试只证明合同回归，不能替代产品准确率。

## 12. V2（方案 A，2026-08-29 已批准实施）

V2 的目标不是取消所有澄清，而是消灭可以由现有受治理证据、受限 AI 与确定性
校验共同解决的澄清。普通用户永远不选择 QueryScope；一个请求最多返回一张阻断式
确认卡，卡片只表达业务语义或事实粒度。Scope、Dataset、root model、Route、Join 和
内部语义 ID 仍只存在于服务端与高级诊断。

### 12.0 冻结的阶段与统一决策链

结构化路径保持不变：

```text
QueryStructReq -> StructQueryParser -> semantic Translator
```

自然语言顶层阶段保持不变：

```text
PRECHECK
  -> CANDIDATE_DISCOVERY
       -> 一次全局 Mapper evidence
       -> exact / human / memory / bounded-AI 业务语义决策
       -> QueryScopeResolver 确定性复核
       -> Rule discovery for the selected Scope
  -> FINAL_PARSING
       -> LLMSqlParser / RuleSqlParser（既有固定顺序）
       -> authoritative parsed/corrected textual S2SQL
       -> Correctors
       -> SqlQueryParser / expression parsers / OntologyQueryParser
       -> semantic Translator
       -> decision settlement（只验收或拒绝，不改写 S2SQL）
  -> TRANSLATING / SQL_GUARD / EXECUTING / POST_PROCESSING
```

裁决不得新增顶层 QueryStage，不得生成 SemanticQuery、S2SQL、QueryType、过滤、聚合、
排序或 Join。它只能从本轮 Mapper 已产生、Release 已发布的候选中选择一个本地候选键。

### 12.0.1 一次确认合同

- 有 weak semantic 候选时先解决语义；选中的 Metric owner 同时决定事实根，不得再问
  一次业务对象。
- 没有 Metric 锚点时才生成安全、可区分的业务意图候选；AI 选择的是业务意图键，
  不是 Dataset/Scope/root ID，随后仍由 QueryScopeResolver 恢复并复核唯一 Scope。
- AI `AMBIGUOUS | NONE | UNAVAILABLE`、非法键、超时、候选溢出或 Resolver 不唯一时，
  回退同一张人工卡；不得尝试第二名或按分数选第一个。
- 普通 wire 只含 opaque `candidate_id`、`kind`、`label`、`description`。一个 token 可以
  同时绑定语义选择和内部 Scope，但客户端不得解析。
- 用户点击一次后必须直接进入原 FINAL_PARSING，或返回稳定治理拒绝；不得再次显示
  `analysis_object` 卡形成两跳循环。

### 12.0.2 决策权力与 provenance

决策来源必须分开记录：`exact | human | memory | ai | final_llm`。AI 或记忆不得伪装成
`exact_metric_ids`，人工选择不得伪装成 AI。事实根优先级固定为：

```text
direct exact metric
  > release-bound human confirmation
  > valid confirmation memory
  > bounded AI MATCH + Resolver SELECTED
  > no automatic decision
```

精确 Dimension/DimensionValue、冻结 Route、扇出、版本和跨事实根门禁在任何来源下都
保留否决权。多个独立指标落在不同事实根继续返回
`CROSS_FACT_METRICS_UNSUPPORTED`。

### 12.0.3 final settlement（第二把钥匙）

任何决定事实根或由人工/记忆/AI 明确选择的 weak semantic 都形成 settlement obligation：

- `MUST_USE_ONE`：最终 authoritative S2SQL 必须实际使用选中的一个 Metric/Dimension；
  0 个或多个都不能执行。
- `AT_MOST_ONE`：同一弱短语只允许最终使用至多一个存活候选；多个拒绝。
- `MUST_PRESERVE_VALUE`：沿用 exact dimension-value grounding。
- `ROOT_MUST_MATCH`：最终 Dataset root 必须等于 Resolver 已复核 owner。

settlement 读取 Translator 已解析的受治理语义查询或等价 audit projection，只能接受、
澄清或拒绝，绝不补造或改写 textual S2SQL。存在未满足的 `MUST_USE_ONE` 时，Rule fallback
不得凭一个可执行数字替代业务裁决。

### 12.1 weak semantic 与跨类型裁决

方案 A 把“含 Metric 锚点”的单短语裁决扩展为 typed semantic candidate 裁决：同一个
normalized detected phrase 内可以同时出现 Metric、Dimension 或 DimensionValue；多个独立
Metric weak phrase 以一个有界结构化请求批量裁决，每个 phrase 独立返回候选键。所有已解决
Metric 必须落在同一事实根，否则沿既有跨事实根合同拒绝。模型未完整裁决时，人工卡按 phrase
生成安全组合，一个 opaque token 同时携带每个 phrase 的选择，最终逐组 settlement。

纯 Dimension ↔ DimensionValue（候选组内没有 Metric 锚点）和多短语 mixed-type 批量自动
裁决属于方案 B，当前保持既有 final-LLM、精确值 grounding 与 fail-closed 行为，不得借方案 A
的开关静默上线。方案 B 开发前需单独冻结多元素 token、事实根复核、operator/value settlement
和 product campaign cohort；本节不把它宣称为已实现。

V1 单 weak Metric 继续由 `weak_metric_adjudication_mode` 控制；跨类型与多 Metric phrase
使用独立 `semantic_intent_adjudication_mode=off|shadow|auto`。升级时后者默认 `shadow`，
不得继承旧部署已经放开的单指标 `auto` 权限。

owner A 已由 direct exact / confirmed / memory / AI metric 等证据确定后，A 内 weak
dimension/metric 候选可交给最终 LLM 做后置选择。该能力只能在 A 内发生：

- 不得让 weak semantic 或 LLM 切换事实根。
- LLM 只使用 A 内已发布的业务名和别名表达选择，不输出内部 ID。
- Corrector 和 Translator 后确定性检查实际使用了哪个已治理成员；`MUST_USE_ONE` 的
  0 个或多个稳定澄清，`AT_MOST_ONE` 的多个稳定拒绝。
- 不得用 score-gap、关键词或位置规则补齐 LLM 未表达的语义。
- 需要先补齐 stage、输入/输出与失败行为的评审合同，再开始编码。

### 12.2 业务意图裁决

仅在没有 Metric 锚点、Resolver 得到两个及以上真实可行且业务文案可区分的事实根时调用。
模型只看到原问题、exact 维度/值业务文本，以及每个候选 root 的业务名称、别名、说明、
事实粒度和有界语义摘要。模型返回本轮局部键；服务端恢复 root 后必须重新运行同一 Resolver。

`analysis_object_adjudication_mode=off|shadow|auto` 与 weak semantic 开关独立：

- `shadow` 记录决定但仍返回 V1 业务卡；
- `auto` 只采用合法 `MATCH + Resolver SELECTED`；
- 其它结果回退卡片，普通响应不出现 Scope/Dataset/root/Route/Join。

模型功能上参与业务意图判断，但不直接选择 QueryScope。这个受限例外由用户于
2026-08-29 明确批准，替代此前“LLM 不得影响事实根”的绝对禁止；确定性 Resolver 和
所有治理否决权不变。

### 12.3 确认记忆

用户显式确认或结果 chip 的显式切换可以保存为有作用域、可撤销、可过期的确认记忆。
至少绑定 actor、project、Release ID、spec hash、index snapshot、normalized detected phrase、
element type/id、候选集 hash、exact-context hash、创建/过期时间。读取发生在当前 Mapper
候选集形成之后，因此记忆只能选择本轮仍存在的候选，不能扩展语义。

AI 自动选择不得写入长期记忆；跨 actor/project/version、候选消失、候选集变化、冲突、过期
或已撤销全部弃权。记忆命中保留独立 `memory_confirmed` provenance，不伪装成人工当前选择。

### 12.4 确认到 alias / Term 的治理闭环

重复确认可生成“待审核” alias 或 Term 建议，但不能直接修改 Active Release：

```text
线上确认事件
  -> 聚合为建模建议（带来源、次数、版本和冲突证据）
  -> 人工审核
  -> Candidate Revision
  -> 发布校验
  -> 新 Release 中成为 direct exact / governed Term
```

一次线上选择不是长期业务事实。跨用户冲突、同名跨 owner 冲突和低频偶然选择必须保留为审核证据，禁止自动发布。

### 12.5 阈值与自动采用离线校准

Embedding 阈值只能基于版本化、隐藏标签的离线数据校准，并分别观察召回率、候选数、确认完成率和误候选率。阈值必须连同模型 ID、归一化公式、数据版本与评审结论一起版本化；禁止按单个在线问题动态降阈值，也禁止把新阈值升级成自动确认权限。

### 12.6 V2 自动放量门槛

V2 编码前必须同时具备：

1. 修复 product campaign 对普通 option `dataset_id` 的过期依赖；评测只能通过 fixture-side
   业务真值匹配 opaque token 或 privileged diagnostics，不得恢复普通 wire 泄漏。
2. V1 线上/隐藏套件基线与典型失败样本。
3. settlement、记忆、学习闭环的错误与撤销合同测试齐全。
4. 每个自动 cohort 的 `v1_pass_v2_wrong == 0`、silent wrong 不增加、false accept 不增加，
   且最终结果与 reference SQL/结果一致。
5. 明确的 shadow、selective auto、回滚和历史记忆失效策略；样本不足的 cohort 保持 shadow。

## 13. 版本演进规则

- V1 行为变更必须更新本文档版本、对应合同测试和 `CLAUDE.md` 中的已评审行为规格。
- V2 任一子能力进入开发前，应从 Deferred 拆成独立、已评审的行为版本，不得以“优化准确率”名义混入 V1 修复。
- 任何阈值、证据权力或失败行为变化都属于产品合同变化，必须记录兼容性影响。
- 高级诊断可以增加观察字段，但不得改变在线选择结果；普通用户表面继续保持 Scope 不可见。
