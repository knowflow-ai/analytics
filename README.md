# KnowFlow Analytics

[English](README.en.md) | 简体中文

**面向 PostgreSQL 的开源语义层与受治理问数引擎。**

KnowFlow Analytics 把指标、维度、业务术语、维度值、实体关系和聚合口径维护成一份经过审核、可版本化的语义模型。同一份模型可以服务自然语言问数、AI Agent、结构化查询 API、嵌入式数据应用和回归评测，不需要为每个入口重复维护 Prompt 与 Few-shot。

LLM 只负责把用户意图表达成由业务名组成的语义 SQL（S2SQL）。真正访问数据库的物理表、字段、Join、聚合和参数，由系统根据已发布的语义模型确定性编译。

![KnowFlow Analytics：数据源、语义建模、人工澄清、确认记忆与一键诊断](docs/screenshots/knowflow-analytics-walkthrough.gif)

## 快速开始

准备 Docker，以及可访问的 OpenAI-compatible Chat / Embedding 模型。Compose 会启动 Analytics 和它自己的 catalog PostgreSQL；要分析的业务 PostgreSQL 在页面中配置。

```bash
git clone https://github.com/knowflow-ai/analytics.git
cd analytics
docker compose -f docker-compose.oss.yml up -d
```

打开 <http://localhost:9395>，按页面提示填写业务数据库和两个模型端点。

默认镜像是 [`knowflowai/analytics:v0.0.1`](https://hub.docker.com/r/knowflowai/analytics/tags?name=v0.0.1)，支持 `linux/amd64` 和 `linux/arm64`。

---

## 为什么是语义层，而不是继续调 Text-to-SQL Prompt

典型的 Text-to-SQL 应用把 Schema、业务说明和少量 SQL 样例拼进 Prompt，让模型直接生成物理 SQL。它可以快速完成单点验证，但业务复杂后会遇到三个问题：

1. 业务含义散落在 Prompt 和 Few-shot 中。不同 Agent、页面和服务各维护一份，很快产生口径漂移。
2. 每次提问都要重新推断指标、聚合、Join 和事实粒度。Prompt 可以引导模型，却不能强制这些规则始终成立。
3. 修复通常只覆盖当前问法。换一种表达、组合两个已有指标，或者更换模型，原来的样例未必继续有效。

语义层把这类知识从 Prompt 中拿出来，变成独立于问题和模型的受治理资源：

| 关注点 | 直接 Text-to-SQL | KnowFlow Analytics |
|---|---|---|
| 业务定义 | 写在 Prompt、文档片段或 Few-shot 中 | 指标、维度、术语和值字典进入版本化 Catalog |
| Join 与粒度 | 模型根据 Schema 临时推断 | 人工确认关系基数，发布时冻结安全路径和事实根 |
| SQL 生成 | LLM 直接输出物理 SQL | LLM 输出业务名 S2SQL，Translator 编译参数化物理 SQL |
| 歧义处理 | 改 Prompt、增加样例或静默选一个 | 展示业务候选，人工确认；确认可在严格上下文内记忆 |
| 复用范围 | 通常绑定某个 Agent 或问数入口 | 同一 Release 服务 Agent、UI、API、评测和其它数据应用 |
| 变更控制 | Prompt 修改后整体行为可能漂移 | Candidate Revision 审核、校验、发布、回滚 |
| 失败策略 | SQL 合法就可能执行 | 成员、路径、聚合或版本无法证明时 fail closed |

### 语义层带来的泛化

这里的“泛化”不是让模型在未知业务里猜得更大胆，而是在已经治理的业务边界内复用稳定语义：

- “销售额”“营收”“GMV”可以映射到同一个指标，新增问法通常只需补业务词典，不用重写整套 Prompt。
- 已定义的指标、维度、过滤值和时间口径可以自由组合，覆盖建模时没有逐题枚举的问题。
- Agent、问数页面和结构化 API 读取同一份 Release，不会因为入口不同而使用不同口径。
- 更换 Chat 模型后，指标公式、Join 路径、聚合方式和执行边界仍由语义层控制。

Few-shot 仍然有价值，但它适合帮助模型学习表达形式，不应承担业务事实和执行规则的所有权。

### 与 Cube、Wren AI 的关系

[Cube Core](https://github.com/cube-js/cube) 把指标、维度、Join 和访问规则定义一次，再通过 SQL、REST、GraphQL 提供给 BI、应用和 AI Agent。[Wren AI](https://github.com/Canner/WrenAI) 也通过 MDL 与 context layer，让业务定义、样例、记忆和治理信息能够被 Agent 复用。

KnowFlow Analytics 与它们遵循相同的基本方向：**语义模型是可复用的数据基础设施，不是某次 LLM 调用的附件。** 当前项目更聚焦以下产品选择：

- AI 辅助完成初始建模，但产出始终是可逐项审核的草案。
- LLM 不生成最终物理 SQL，执行 SQL 由语义模型确定性编译。
- 发布前同时检查模型结构和真实数据，包括关系基数、扇出、主标识与指标样本。
- 自然语言有歧义时显式确认，并把人工选择保存为有版本和上下文边界的记忆。
- 每次查询都有固定阶段诊断，可导出脱敏 Markdown 报告。

KnowFlow Analytics 当前只支持 PostgreSQL 数据源。它不是 Cube 或 Wren AI 的协议实现，也不要求引入它们的运行时。

---

## 一份语义模型，可以服务哪些场景

```mermaid
graph LR
    DB["PostgreSQL"] --> C["受治理语义 Catalog"]
    C --> R["不可变 Release"]
    R --> A["AI Agent / Tool"]
    R --> N["自然语言问数"]
    R --> S["结构化查询 API"]
    R --> E["嵌入式数据应用"]
    R --> Q["评测与质量门禁"]
```

- **AI Agent**：Agent 通过资源 API 获取业务定义并发起受治理查询，不需要自己拼物理表、列和 Join。
- **自然语言问数**：内置 UI 负责建模、发布前试问、线上提问、澄清和诊断。
- **结构化查询**：调用方直接提交已治理的指标、维度和过滤条件，跳过自然语言理解。
- **嵌入式应用**：业务系统可以把 Analytics 作为独立查询后端，复用同一套指标和权限边界。
- **质量工程**：Golden Suite、真实数据质量报告和查询失败记录都绑定同一 Revision / Release。

这也是语义层相对单点 Text-to-SQL 的主要价值：建模成本只付一次，治理结果可以被多个消费者长期复用。

---

## 核心设计

### 1. 统一语义目录

目录包含 Model、Relation、Metric、Dimension、Term 和 DimensionValue。业务名称、别名、指标公式、默认聚合、可加性、时间轴和真实维度值都属于模型数据，而不是藏在 Prompt 里。

### 2. AI 建模，但不让 AI 直接发布

AI 可以建议实体名、字段角色、指标、维度和别名。建议先进入 Candidate Revision；覆盖人工内容的建议默认不选中，必须经过审核、结构校验和发布。

### 3. 查询作用域由编译器生成

每个查询作用域冻结一个事实根、明确的指标与维度成员，以及从事实根出发的安全 Join 路径。用户维护语义目录，不需要再维护第二套“主题”配置。

### 4. LLM 只生成语义 SQL

```text
自然语言问题
  → Mapper：说法映射到已发布业务名
  → Parser：生成只包含业务名的 S2SQL
  → Corrector：校验成员、过滤、聚合和确认义务
  → Translator：绑定冻结路径，编译参数化物理 SQL
  → Guard：只读 AST 白名单、结果上限和执行超时
  → PostgreSQL
```

如果 S2SQL 引用了未发布名称、丢失已确认过滤值、跨越不安全关系或没有落实人工选择，查询不会进入数据库。

### 5. 人工澄清与确认记忆

普通问数最多展示一张业务确认卡，不暴露内部 Scope、Dataset 或语义 ID。人工选择只有在最终 S2SQL 确实使用该语义、且查询成功执行后才会写入记忆。

记忆绑定用户、项目、Release、语义索引、原短语、候选集、精确上下文和 TTL。版本、候选或上下文变化时自动失效；AI 自动选择不会写长期记忆。

详细设计见 [`docs/semantic-confirmation-and-scope-routing.md`](docs/semantic-confirmation-and-scope-routing.md)。

### 6. 发布前验证与线上诊断

- 结构化 Playground 用来验证模型与 SQL 翻译。
- 自然语言 Playground 用来验证别名、映射和完整查询链。
- 数据质量报告使用真实只读查询检查主标识、关系基数、扇出、指标样本和可达性。
- 一键诊断展示固定阶段时间线，并导出脱敏 Markdown。

诊断详细合同见 [`docs/one-click-query-diagnostics.md`](docs/one-click-query-diagnostics.md)。

---

## 产品能力

| 类别 | 当前能力 |
|---|---|
| 数据建模 | Schema 快照、漂移检测、关系画布、人工基数确认、SQL Model |
| AI 建模 | 实体/字段命名、角色分类、指标与维度草案、别名和值字典建议 |
| 指标治理 | 原子/派生指标、默认聚合、展示格式、半可加约束、指标时间轴 |
| 查询编译 | S2SQL、冻结 Join 路径、参数化 SQL、只读 Guard |
| 高级查询 | Set operations、同比/环比、滚动比率、组内占比 |
| 歧义治理 | 同名冲突、业务候选卡、受限 AI 裁决、人工确认记忆 |
| 版本管理 | Revision、ETag、不可变 Release、语义索引绑定、回滚 |
| 质量保障 | 双模式 Playground、Golden Suite、真实数据质量报告 |
| 可观测性 | 固定查询时间线、失败记录、脱敏 Markdown 诊断导出 |
| 部署 | 独立 Web UI、Docker Compose、OpenAI-compatible 模型端点 |

---

## 实测结果

`scripts/product_accuracy_campaign.py` 走与浏览器相同的鉴权 API：原始 PostgreSQL → 导入 → 关系确认 → AI 建模 → 发布 → 发布后加载隐藏问题 → 自然语言试问 → 与参考 SQL 结果逐行比较。

当前三套中文小型回归集共 36 题：

| 数据集 | 结构 | 正确 | 准确率 | 静默错答 |
|---|---:|---:|---:|---:|
| 电商双十一 | 6 表 | 12 / 12 | 100% | 0 |
| 城市与图书馆 | 3 表 | 11 / 12 | 91.7% | 0 |
| 音乐 Holdout | 6 表、8 外键、3 主键 | 9 / 12 | 75% | 0 |
| **合计** | 15 表 | **32 / 36** | **88.9%** | **0** |

这些数字是产品链回归证据，不是通用 Text-to-SQL 基准结论。题集规模仍小，且每轮会重新执行 AI 建模。城市集的一次失败来自 HTTP 传输；音乐集的失败包括参考聚合口径分歧，以及无主标识、无业务度量的事件表无法成为事实根。

项目更关注“静默错答”而不只是完成率：拒绝或澄清对用户可见，一个看起来正常的错误数字不可见。

---

## 使用流程

```text
连接数据源
  → 导入表并确认实体关系
  → AI 生成建模草案
  → 审核指标、维度、术语和值字典
  → 运行结构校验与真实数据质量报告
  → 用两个 Playground 试问
  → 发布不可变 Release
  → Agent、UI 或 API 开始查询
```

内置界面包含三个主要建模入口：实体与关系、业务词典、目录总览。查询作用域只在高级诊断中展示。

---

## 配置与运维

### Compose 参数

| 变量 | 默认值 | 作用 |
|---|---|---|
| `KNOWFLOW_ANALYTICS_IMAGE` | `knowflowai/analytics:v0.0.1` | 要运行的 Analytics 镜像 |
| `KNOWFLOW_OSS_BIND_ADDRESS` | `127.0.0.1` | 宿主机监听地址 |
| `KNOWFLOW_OSS_PORT` | `9395` | 宿主机端口 |
| `CATALOG_DB_PASSWORD` | `analytics` | 内置 catalog PostgreSQL 密码，必须 URL-safe |
| `KNOWFLOW_OSS_ACCESS_PASSWORD` | 空 | 独立版共享访问口令 |

远程部署时再把监听地址设为 `0.0.0.0`，同时配置访问口令、HTTPS 反向代理和防火墙：

```bash
KNOWFLOW_OSS_BIND_ADDRESS=0.0.0.0 \
KNOWFLOW_OSS_ACCESS_PASSWORD='replace-me' \
docker compose -f docker-compose.oss.yml up -d
```

`KNOWFLOW_OSS_ACCESS_PASSWORD` 是单用户共享口令，不是多用户认证或 RBAC。

### 日常命令

```bash
docker compose -f docker-compose.oss.yml logs -f analytics
docker compose -f docker-compose.oss.yml restart analytics
docker compose -f docker-compose.oss.yml down       # 保留数据
```

状态存放在两个 volume：`catalog-data` 保存语义资源、Revision 和 Release，`analytics-data` 保存独立版设置。它们不复制业务库数据。

容器连接宿主机 PostgreSQL 时使用：

```text
postgresql://user:password@host.docker.internal:5432/your_database
```

<details>
<summary>更多运行参数</summary>

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KNOWFLOW_OSS_CATALOG_DATABASE_URL` | 必填 | 服务自己的 catalog 库 |
| `KNOWFLOW_OSS_DATA_DIR` | `./data` | 独立版设置目录 |
| `KNOWFLOW_OSS_MODELING_MAX_CONCURRENCY` | `3` | AI 建模并发数 |
| `KNOWFLOW_OSS_MODEL_TIMEOUT_SECONDS` | `120` | 单次模型调用超时 |
| `KNOWFLOW_OSS_MODELING_SAMPLE_VALUES` | `true` | 是否采样低基数维度值 |
| `KNOWFLOW_OSS_MULTI_TURN_ENABLED` | `false` | 是否启用追问改写 |
| `KNOWFLOW_OSS_SELF_CONSISTENCY_NUMBER` | `1` | S2SQL 多次生成投票，`1` 表示关闭 |
| `KNOWFLOW_OSS_WEAK_METRIC_ADJUDICATION_MODE` | `shadow` | 单弱指标 AI 裁决模式 |
| `KNOWFLOW_OSS_SEMANTIC_INTENT_ADJUDICATION_MODE` | `shadow` | 跨类型/多短语裁决模式 |
| `KNOWFLOW_OSS_ANALYSIS_OBJECT_ADJUDICATION_MODE` | `shadow` | 多业务粒度裁决模式 |
| `KNOWFLOW_OSS_CONFIRMATION_MEMORY_TTL_SECONDS` | `2592000` | 人工确认记忆 TTL |
| `KNOWFLOW_OSS_ALLOW_DEBUG_SQL` | `true` | 是否允许授权诊断返回物理 SQL |

三个 AI 裁决开关默认均为 `shadow`：系统记录判断，但仍由用户确认。请用自己的真实问题完成校准后再考虑 `auto`。

</details>

---

## 从源码开发

需要 Python 3.12+、Node 18+、[uv](https://docs.astral.sh/uv/) 和 PostgreSQL。

```bash
uv sync --python 3.12 --all-extras
(cd web && npm install)

createdb analytics_catalog
cp .env.oss.example .env
# 编辑 KNOWFLOW_OSS_CATALOG_DATABASE_URL
set -a; source .env; set +a

# 终端 1
uv run knowflow-analytics-oss

# 终端 2
cd web && npm run dev
```

自己构建独立镜像：

```bash
docker build -f Dockerfile.oss -t knowflowai/analytics:local .
KNOWFLOW_ANALYTICS_IMAGE=knowflowai/analytics:local \
docker compose -f docker-compose.oss.yml up -d
```

测试：

```bash
uv run pytest
uv run ruff check src tests
(cd web && npx vitest run)
```

### 代码地图

```text
src/knowflow_analytics/
├── api.py             # 项目、Revision、Catalog、发布和查询 API
├── application.py     # 用例编排与产品不变量
├── catalog/           # PostgreSQL 持久化与 Release
├── modeling/          # 内省、AI 建模、质量报告和 Scope 编译
├── query/             # Mapper、Parser、Corrector、澄清与诊断
├── semantic/          # 语义索引与 S2SQL Translator
├── execution/         # SQL Guard 与只读 PostgreSQL 执行
├── gateways/          # Chat、Embedding、Knowledge 网关
└── oss/               # 独立版配置、鉴权和静态托管
```

改查询管道前，先阅读 `tests/unit/test_query_stage_pipeline.py`、`tests/unit/test_textual_s2sql_pipeline.py` 和相关设计文档。自然语言与结构化查询是两条独立合同，不能为了实现方便合并。

---

## 数据与安全边界

- 物理 SQL 参数化，并经过 AST 白名单、只读事务和执行限制。
- 查询结果直接返回调用方，不会再发给模型生成总结。
- 建模时可采样低基数维度值，用于值字典和别名建议；可通过配置关闭。
- 普通业务答案不暴露内部 Scope、Dataset、语义 ID 或物理 SQL。
- 诊断导出会脱敏密码、API key、连接串凭据和确认 token。
- 独立版配置文件权限为 `0600`；`.env`、`local.env` 和 `data/` 均被 Git 与 Docker 构建上下文排除。

生产环境仍应使用专用只读数据库账号，并在服务外配置网络隔离、TLS 和正式身份认证。

---

## 当前边界

- 数据源仅支持 PostgreSQL。
- 独立版是单用户 + 可选共享口令，不提供多用户 RBAC 或行列权限。
- AI 建模在请求周期内同步执行，大型 Schema 需要分批导入或调高超时。
- 同一实体存在两条等长最短 Join 路径时，该实体不会自动进入作用域。
- 既无主标识也无业务度量的事件/桥接表不会成为事实根。
- 多轮改写默认关闭，只读取同一会话、Release 和业务范围内上一轮成功的语义查询。
- 确认记忆支持 API 查询和撤销，独立版暂未提供单独的记忆管理页面。

---

## 贡献与许可

Issue 和 Pull Request 都欢迎。涉及 Mapper、Parser、S2SQL、Corrector、Translator、路由或执行的改动，请说明所影响的阶段、输入输出和失败行为，并补对应契约测试。不要在运行时代码中加入面向某个数据集、业务名或基准题措辞的修复分支。

KnowFlow Analytics 使用 [Apache License 2.0](LICENSE)。
