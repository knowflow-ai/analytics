# SuperSonic Parser fallback 对齐合同

状态：2026-08-29 用户已评审并批准实施。

## 冻结的自然语言阶段顺序

本变更只修复 Parser candidate 准入与同一 Parser 链在 ALL 重试中的完整性，
不合并、跳过、重排或改变任何阶段的权威：

```text
自然语言问题
  -> NL2SQLParser 外层编排
  -> Mapper
  -> RuleSqlParser candidate discovery
  -> selected SemanticParseInfo
  -> final Mapping（选中 Scope）
  -> LLMSqlParser
       -> LLM response candidate admission
       -> 没有形成 candidate 时，同一次 pass 才运行 RuleSqlParser
  -> QueryTypeParser
  -> registered textual Correctors
  -> SqlVariableParser
  -> StructQueryParser
  -> SqlQueryParser
  -> DefaultDimValueParser
  -> DimExpressionParser
  -> MetricExpressionParser
  -> MetricRatioParser
  -> OntologyQueryParser
  -> semantic Translator
```

若上述完整 final pass 返回失败，外层只把 Mapping 切换为 `ALL`，然后重新运行
同一条 Parser 链：

```text
ALL Mapping
  -> LLMSqlParser
       -> 没有形成 candidate 时运行 RuleSqlParser（使用 ALL Mapping）
  -> QueryTypeParser / Correctors
  -> SqlVariableParser / StructQueryParser / SqlQueryParser
  -> DefaultDimValueParser / DimExpressionParser / MetricExpressionParser
  -> MetricRatioParser / OntologyQueryParser
  -> semantic Translator
```

结构化查询仍独立走 `QueryStructReq -> StructQueryParser -> semantic Translator`，
不经过本合同。

## Candidate 准入合同

输入是 LLM 返回的 textual S2SQL；输出只能是一个通过普通 SQL 结构校验的
textual candidate，文本仍是后续链路的唯一权威。

与 SuperSonic 固定的 JSQLParser 4.9 行为一致，每个 `SELECT` 必须至少包含一个
投影项。SQLGlot 能构造 AST、但没有投影项的残缺文本（例如 `SELECT`、
`SELECT FROM t`、外层为空的 `WITH ... SELECT`）在准入检查处以既有
`LLM_S2SQL_AST_INVALID` 拒绝；LLMSqlParser 重试耗尽后仍按既有
`LLM_S2SQL_INVALID` 表示没有形成 candidate，并交回同一次 pass 的 RuleSqlParser。

该校验不选择 Dataset、Metric、Dimension、过滤、聚合或 QueryType，也不把
残缺 SQL 补成新语义。

## Fallback 合同

- LLM 没有形成 candidate：同一次 pass 的 RuleSqlParser 可以接管。
- LLM 已形成 candidate：同一次 pass 的 RuleSqlParser 必须退出；Corrector 或
  Translator 后续拒绝不得复活 discovery Rule candidate。
- 完整 final pass 失败：外层进入 ALL retry。
- ALL retry 仍是 `LLM_OR_RULE`：ALL-LLM 没有形成 candidate 时，RuleSqlParser
  使用本次 ALL Mapping 重新生成 candidate；不得复用旧 discovery candidate。
- ALL-LLM 已形成 candidate、但后续被拒绝：不得再运行 ALL-Rule。
- ALL-Rule 没有从本次 Mapping 生成 candidate：保留最后一个 LLM 失败并正常结束
  FINAL_PARSING，不得把 `None` 当 candidate 或产生内部错误。
- 治理阻断错误继续 fail-closed，不得由 Rule fallback 绕过。

## 兼容性证明

合同测试必须同时固定：

1. 空投影 `SELECT` 的空白、大小写、注释和嵌套变体都在 candidate 入队前拒绝；
2. 至少一个投影项的普通查询仍可形成 LLM candidate；
3. 首次 LLM 无 candidate 时 Rule 接管；
4. 合法 LLM candidate 后续失败仍只进入 ALL retry，不复活 discovery Rule；
5. ALL-LLM 无 candidate 时使用 `MapMode.ALL` 生成新的 Rule candidate；
6. ALL-LLM 已形成 candidate 后续失败时仍不运行 Rule。
