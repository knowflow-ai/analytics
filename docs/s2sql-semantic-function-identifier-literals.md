# S2SQL 语义函数标识符字面量兼容合同

状态：2026-08-28 用户已评审并批准实施。

## 问题

自然语言链路要求 LLM 生成 textual S2SQL。SQL 中双引号表示标识符，单引号表示字符串值；但模型偶尔会把 Catalog 示例中的单引号复制到 `RATIO_TO_TOTAL` 的指标和维度参数：

```sql
SELECT RATIO_TO_TOTAL('净收入', '地区', '华东') FROM '销售经营'
```

函数签名已经固定前三个位置的含义为 `metric identifier`、`dimension identifier`、`value literal`。如果前两个名称确实属于当前已选 Dataset，拒绝整个候选只是在惩罚引号形态，不代表业务语义不确定。

## 所属阶段与冻结边界

该兼容仅位于自然语言管线的 `FINAL_PARSING`：LLM JSON 解码之后、既有 textual S2SQL 校验之前。

```text
LLMSqlParser 输出 textual S2SQL
  -> identifier quote normalization
  -> 本合同的 semantic-function identifier-literal normalization
  -> validate_textual_s2sql
  -> exact-value grounding
  -> 后续 Correctors / SqlQueryParser / Translator
```

它不改变 Mapper、QueryScope 选择、Parser 顺序、Rule fallback、Corrector 顺序或结构化查询管线。规范化后的 textual S2SQL 仍是下游唯一权威。

## 输入、输出与失败合同

仅处理恰好三个参数的 `RATIO_TO_TOTAL`：

1. 第一个参数必须是单引号字符串，并在当前 Release + Dataset 的 `SemanticSymbolTable` 中唯一解析为 Metric。
2. 第二个参数必须是单引号字符串，并唯一解析为 Dimension。
3. 两项同时成立时，只把前两个位置改成双引号标识符。
4. 第三个参数始终按值 literal 原样保留，包括引号和内容。
5. 任一名称未知、歧义、类型颠倒或不是字符串时，不做部分改写，原文本进入既有校验、重试或失败路径。
6. 其它函数、普通 SELECT 字符串、过滤值和任意其它位置不受影响。

示例：

```sql
-- 接受并规范化
RATIO_TO_TOTAL('净收入', '地区', '华东')
-> RATIO_TO_TOTAL("净收入", "地区", '华东')

-- 类型颠倒：不改写，继续按既有合同失败
RATIO_TO_TOTAL('地区', '净收入', '华东')

-- 未治理名称：不改写，继续按既有合同失败
RATIO_TO_TOTAL('临时口径', '地区', '华东')
```

## 安全与治理边界

- 名称解析只消费当前已发布且已选 Dataset 的符号表，不跨 Scope 猜测。
- 不依据问题文字、embedding 分数、位置邻近或名称相似度选择语义对象。
- 不生成缺失的指标、维度、过滤、聚合或查询类型。
- 不把业务名称替换成内部语义 ID。
- 精确值 grounding、AST allowlist、只读 Guard、参数化 SQL 和执行限制保持不变。

## 合同测试

`tests/unit/test_query_parser.py` 覆盖：

- 单引号指标/维度成功规范化，值 literal 保真；
- Catalog 业务名重命名后的同构行为；
- 未知、歧义和指标/维度类型颠倒不被修复；
- 嵌套 CTE 中的语义函数；
- 非 ASCII、转义字符等值 literal 不被改成标识符；
- textual parsedS2SQL/correctedS2SQL 继续进入原有下游链路。
