# 一键问数诊断与 Markdown 导出

状态：已评审，2026-08-29 用户授权实施。

## 目标

开发者看到自然语言问数结果不符合预期时，可以从当前答案一键打开固定流程时间轴，快速判断问题发生在建模、语义映射、Scope 路由、S2SQL、翻译、安全检查还是数据库执行，并导出一份可直接附在 Issue 中的 Markdown 报告。

报告分两层：

1. 顶部是固定阶段、首个异常点、阶段状态和关键产物摘要，几秒内可以定位。
2. 后部是当次建模、Revision、请求、响应和完整阶段证据，供深入排查。

## 不改变查询管线

诊断记录器是查询完成后的旁路观察者。它不得修改请求、候选、Scope、S2SQL、Corrector、Route、物理 SQL 或结果，也不得因保存失败改变用户收到的响应。

自然语言管线保持：

```text
NL2SQLParser
  -> Mapper + selected SemanticParseInfo
  -> LLMSqlParser / RuleSqlParser（既有固定顺序）
  -> textual parsedS2SQL / correctedS2SQL（继续是权威）
  -> QueryTypeParser + registered Correctors
  -> SqlQueryParser
  -> DimExpressionParser / MetricExpressionParser / MetricRatioParser
  -> OntologyQueryParser
  -> semantic Translator
```

结构化管线保持：

```text
QueryStructReq -> StructQueryParser -> semantic Translator
```

一键诊断不会把两条管线合并，也不会从诊断内容反向生成或修复查询语义。

## 固定时间轴

建模上下文与本次查询阶段必须明确分层，避免让开发者误以为 AI 建模会在每次问数时重新运行。

```text
上下文（非本次查询阶段）
  0. 数据源与 AI 建模
  1. Revision / Release 冻结版本

本次查询（QueryStage 固定顺序）
  2. PRECHECK
  3. CANDIDATE_DISCOVERY
  4. FINAL_PARSING
  5. S2SQL_CORRECTING
  6. ROUTE_BINDING
  7. TRANSLATING
  8. PHYSICAL_SQL_CORRECTING
  9. PHYSICAL_SQL_VALIDATING
 10. EXECUTING
 11. POST_PROCESSING
 12. FINISHED
```

每个位置始终出现，状态只允许：

- `completed`：已完成，并展示产物。
- `failed`：本阶段失败，并展示错误和已有产物。
- `clarification`：本阶段要求业务确认。
- `started`：记录中只有开始事件。
- `not_run`：前序已经终止，或该管线不经过此阶段。
- `not_recorded`：查询经过了后续阶段，但本阶段没有可观察事件。

同一阶段可以有多条事件，例如安全预检与数据库 dry-run 都可能写入 `PHYSICAL_SQL_VALIDATING`。渲染器必须按原始顺序保留事件列表，禁止用字典覆盖。

## 诊断产物

服务端在 QueryResponse 形成后保存有界、追加式的 `QueryDiagnosticArtifact`：

- `query_id`、actor、project、created_at、expires_at。
- natural / structured 管线类型。
- 问题与请求的安全投影。
- Release、Revision、spec、index、schema snapshot 绑定信息。
- 完整 `QueryTraceStep` 序列和服务端诊断。
- parsed S2SQL、corrected S2SQL、SemanticQuery、物理 SQL（仅在部署允许 debug SQL 时）。
- 数据库执行的列、行数和截断状态；生产默认不持久化结果行，显式受控配置才允许有界样本。
- Completed / Failed / Clarification 的原始响应安全投影。

导出时再读取与产物绑定的 Revision、Catalog、建模建议、建模诊断和可获得的 AI Modeling Job / Proposal。若 Draft Revision 已漂移，报告必须标记版本不一致，不能把当前内容冒充当次内容。

## 快速定位

快速定位只做确定性归纳，不让新的 LLM 猜根因：

- 有 `failed` 或 `clarification` 时，定位到第一条终止事件及服务端 `diagnostics`。
- `COMPLETED` 时明确写“系统链路完成，需核对治理口径、S2SQL、物理 SQL 与结果”。
- 同时列出本次 Metric 的 Catalog 默认聚合与 SemanticQuery 实际聚合，便于发现 SUM / AVG 等口径差异。
- 没有证据时写“未知”，不得从问题关键词补造原因。

## API

```text
GET /v1/analytics/projects/{project_id}/query-diagnostics/export?query_id={query_id}
```

返回 JSON：

```json
{
  "filename": "knowflow-diagnostic-q_xxx.md",
  "media_type": "text/markdown; charset=utf-8",
  "markdown": "# KnowFlow 问数诊断…",
  "sha256": "sha256:…",
  "summary": {},
  "timeline": []
}
```

OSS 转发器与商业版 `/core` BFF 继续原样透传。读取必须同时匹配 actor、project、当前 permission scope hash 和 query_id；不同用户、项目、权限快照、过期记录统一返回不存在，不泄露对象是否真实存在。

Active Release 的普通 Ask 响应只返回业务名称、结果、业务解释、opaque 续跑 token 和
query_id；Scope/Dataset/root/semantic ID、Trace 细节与 S2SQL/物理 SQL 只写入上述授权诊断
产物。Candidate Revision 的建模工作台 preview 仍可按部署权限展示技术细节。

## 安全与边界

- 永久脱敏 Authorization、Cookie、密码、Secret、API Key、Access Token、连接串凭证和 signed candidate token。
- 物理 SQL 继续服从 `allow_debug_sql`，诊断功能不得绕过部署开关。
- 生产默认结果样本行为是 `0` 行；未知列或未治理输出标签 fail-closed，不把原始列 ID 带到普通响应。
- 结果样本数量、单值长度、Trace 列表、Markdown 总大小和保留期都有硬上限，并在截断处留下说明。
- 诊断写入走有界后台队列，每个 actor/project 最多保留 100 条；队列满时丢弃诊断，绝不阻塞问数响应。
- 后台记录器周期分批物理删除过期记录，即使项目停止产生新查询也会继续清理。
- 报告包含问题、语义目录、SQL 和维度值，顶部必须提示用户分享前检查业务敏感信息。
- 诊断写入失败只记日志，原问数响应照常返回。
- 过期或缺失的 query_id 给出明确的 `QUERY_DIAGNOSTIC_NOT_FOUND`，不重跑查询。

## 失败模式

| 失败 | 行为 |
|---|---|
| 诊断表不可写 | 记录 warning，原查询结果不受影响 |
| Query 已过期 | 导出接口返回明确过期/不存在，不重跑 |
| Draft Revision 已变化 | 报告标 `VERSION_STALE`，保留当次 query 证据 |
| 建模 Job / Proposal 已不存在 | 对应章节标“不可用”，其余报告继续生成 |
| Markdown 达到大小上限 | 有界截断并列出被截断章节 |
| 未允许 debug SQL | 物理 SQL 标“部署未授权导出” |
| 未授权 actor/project | 404 式隐藏，不能旁路读取 |

## NOT in scope

- 不自动调用 LLM 生成根因结论。
- 不重跑原问题来补 Trace。
- 不导出服务日志、环境变量、容器信息或数据库全量数据。
- 不修改 Scope、Mapper、Parser、Corrector 或 Translator 的行为。
- 不在 `/analytics-legacy` 页面（那个旧巨石页已于 2026-09-04 整体删除）。

## 验证

- 单元测试固定阶段顺序、重复阶段、未执行阶段和 Markdown 产物。
- 单元测试所有密钥、连接串、Cookie 和 continuation token 脱敏。
- 存储测试 actor/project/query_id 隔离、TTL 和有界结果样本。
- API 测试成功导出、未授权、过期、Revision 漂移与 debug SQL 门禁。
- 前端测试 Completed / Failed / Clarification 都显示“一键诊断”，固定时间轴可展开并下载 Markdown。
- 真实自然语言试问验证报告对应同一个 query_id，且不改变原查询结果。
