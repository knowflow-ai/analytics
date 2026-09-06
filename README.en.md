# KnowFlow Analytics

English | [简体中文](README.md) | [Website](https://www.knowflowchat.cn) | [Changelog](CHANGELOG.en.md) | [Community and support](#community-and-support)

**An open-source semantic layer and governed query engine for AI agents and data applications.**

KnowFlow Analytics maintains metrics, dimensions, business terms, dimension values, entity relationships, and aggregation rules in a reviewed, versioned semantic model. The same model can serve natural-language analytics, AI agents, structured query APIs, embedded data applications, and regression evaluation. You do not need to rebuild business meaning independently in every prompt and few-shot set.

The LLM only expresses intent as semantic SQL (S2SQL) made of business names. Physical tables, columns, joins, aggregations, and parameters are compiled deterministically from the published semantic model.

![KnowFlow Analytics: datasource import, semantic modelling, human clarification and one-click diagnostics](docs/screenshots/knowflow-analytics-walkthrough.gif)

## Quick start

You need Docker and reachable OpenAI-compatible chat and embedding endpoints. Compose starts Analytics and its catalog PostgreSQL. Fill in the model endpoints under Settings and add the business databases you want to analyse under Database connections (several are supported). You can also upload an Excel file, which becomes a data source of its own.

```bash
git clone https://github.com/knowflow-ai/analytics.git
cd analytics
docker compose -f docker-compose.oss.yml up -d
```

Open <http://localhost:9395> and follow the setup screen to configure the data source and model endpoints.

The default image is [`knowflowai/analytics:v0.0.2`](https://hub.docker.com/r/knowflowai/analytics/tags?name=v0.0.2), published for `linux/amd64` and `linux/arm64`.

---

## Why a semantic layer instead of another Text-to-SQL prompt

A typical Text-to-SQL application puts a schema, business notes, and a few SQL examples into a prompt, then asks the model to emit physical SQL. That is useful for a quick proof of concept. It becomes fragile as the business grows:

1. Business meaning is scattered across prompts and few-shot examples. Each agent, UI, and service soon carries a slightly different definition.
2. Every question requires the model to infer metrics, aggregation, joins, and fact grain again. A prompt can suggest rules but cannot enforce them.
3. A fix often covers one wording. Rephrasing the question, combining two known metrics, or changing the model may bypass the example that made the previous case work.

A semantic layer moves this knowledge out of the prompt and into governed resources that are independent of any single question or model:

| Concern | Direct Text-to-SQL | KnowFlow Analytics |
|---|---|---|
| Business definitions | Prompt text, retrieved notes, or few-shot examples | Metrics, dimensions, terms, and value dictionaries in a versioned catalog |
| Joins and grain | Inferred from the schema at query time | Human-reviewed cardinality, frozen fact roots, and safe paths |
| SQL generation | LLM emits physical SQL | LLM emits business-name S2SQL; the translator compiles parameterized SQL |
| Ambiguity | Add prompt rules/examples or silently choose | Show business candidates, ask the user, and remember the confirmed choice safely |
| Reuse | Usually tied to one agent or analytics surface | One release serves agents, UI, APIs, evaluation, and other data applications |
| Change control | Prompt edits may move behaviour globally | Candidate revision, review, validation, publish, and rollback |
| Failure policy | Syntactically valid SQL may execute | Fail closed when membership, path, aggregation, or version cannot be proven |

### What “generalization” means here

The goal is not to make the model guess more aggressively in an unknown business. It is to reuse stable meaning inside a governed domain:

- “sales”, “revenue”, and “GMV” can map to one metric. A new expression usually becomes a glossary update, not a prompt rewrite.
- Governed metrics, dimensions, values, filters, and time rules can be composed into questions that were never enumerated during modelling.
- Agents, the analytics UI, and structured APIs read the same release instead of carrying separate definitions.
- Changing the chat model does not change metric formulas, join paths, aggregation rules, or execution boundaries.

Few-shot examples still help the model learn expression forms. They should not own business facts or execution policy.

### How this relates to Cube and Wren AI

[Cube Core](https://github.com/cube-js/cube) defines metrics, dimensions, joins, and access rules once, then exposes them over SQL, REST, and GraphQL to BI tools, applications, and AI agents. [Wren AI](https://github.com/Canner/WrenAI) uses MDL and a context layer so business definitions, examples, memory, and governance can be reviewed and reused by agents.

KnowFlow Analytics follows the same basic direction: **the semantic model is reusable data infrastructure, not an attachment to one LLM call.** It currently focuses on a different set of product choices:

- AI helps with initial modelling, but every output remains an itemized reviewable draft.
- The LLM never writes the physical SQL that reaches the database.
- Pre-publish validation checks both model structure and real data, including cardinality, fanout, identifiers, and metric samples.
- Ambiguous questions request an explicit business choice and can remember that choice within strict version and context boundaries.
- Every query has a fixed diagnostic timeline and a redacted Markdown export.

KnowFlow Analytics currently reads PostgreSQL and MySQL data sources plus uploaded spreadsheets. It is not an implementation of Cube or Wren protocols and does not require either runtime.

---

## One semantic model, many consumers

```mermaid
graph LR
    DB["PostgreSQL"] --> C["Governed semantic catalog"]
    C --> R["Immutable release"]
    R --> A["AI agent / tool"]
    R --> N["Natural-language analytics"]
    R --> S["Structured query API"]
    R --> E["Embedded data application"]
    R --> Q["Evaluation and quality gates"]
```

- **AI agents** use the resource API and governed query surface instead of assembling physical tables, columns, and joins themselves.
- **Natural-language analytics** uses the bundled UI for modelling, pre-publish trials, online questions, clarification, and diagnostics.
- **Structured queries** submit governed metrics, dimensions, and filters directly, bypassing natural-language interpretation.
- **Embedded applications** can use Analytics as a query backend and share the same metric definitions and execution boundaries.
- **Quality engineering** binds golden suites, real-data reports, and failed-query evidence to the same revision and release.

That is the main advantage over a point Text-to-SQL solution: modelling cost is paid once, while the governed result can support many consumers over time.

---

## Core design

### 1. One semantic catalog

The catalog contains models, relations, metrics, dimensions, terms, and dimension values. Business names, aliases, metric formulas, default aggregation, additivity, logical time axes, and governed values are model data, not hidden prompt instructions.

### 2. AI modelling without AI publishing

AI may propose entity names, field roles, metrics, dimensions, and aliases. Suggestions enter a candidate revision. Anything that would overwrite human-authored content starts unchecked and must pass review, structural validation, and publishing.

### 3. Compiler-generated query scopes

Each scope freezes one fact root, an explicit metric/dimension membership, and safe join paths from that root. Users maintain the semantic catalog rather than a second set of manually curated “topics”.

A scope is **not picked by a router before the question is answered**. The final LLM sees the union of candidate scope members and writes business-name S2SQL; the compiler then tries a deterministic translation against each real scope. Exactly one success binds it, zero means the question crossed fact roots, and several converge to the coarsest grain. The model proposes and the compiler decides: checking is easier than choosing, and translation already validates membership, frozen-path reachability, and every governance rule.

### 4. The LLM emits semantic SQL only

```text
Natural-language question
  → Mapper: wording to published business names
  → Parser: business-name-only S2SQL
  → Corrector: membership, filters, aggregation, and confirmation obligations
  → Translator: frozen routes to parameterized physical SQL
  → Guard: read-only AST allowlist, row limits, and timeouts
  → PostgreSQL
```

If S2SQL references an unpublished name, drops a confirmed value, crosses an unsafe relationship, or fails to use an explicit user choice, it never reaches the database.

### 5. Human clarification and vocabulary-gap feedback

Clarification is a fallback, not the normal path. An ordinary query shows at most one business confirmation card, only for same-name semantic elements or for one phrase that lands on metrics in different fact roots. It never exposes internal Scope, Dataset, or semantic IDs. A human choice must actually appear in the authoritative S2SQL, or the query does not execute.

Wording the system could not answer is not dropped. Refusals, clarifications, members the model guessed on its own, unknown values, and user likes and dislikes all land in one vocabulary-gap inbox, grouped by wording and fed back to modelling: add one glossary term and the same phrasing answers directly next time. This is more controllable than storing the choice in a memory table, because a glossary term is a reviewable, publishable semantic resource that applies to everyone, and a memory is not.

See [`docs/semantic-confirmation-and-scope-routing.md`](docs/semantic-confirmation-and-scope-routing.md).

### 6. Pre-publish validation and online diagnostics

- Structured Playground validates the model and SQL translation.
- Natural-language Playground validates glossary coverage, mapping, and the full query chain.
- The quality report runs real read-only checks for identifiers, observed cardinality, fanout, metric samples, and reachability.
- One-click diagnostics shows a fixed timeline and exports a redacted Markdown report.

See [`docs/one-click-query-diagnostics.md`](docs/one-click-query-diagnostics.md).

---

## Capabilities

| Area | Current capability |
|---|---|
| Data sources | Multiple PostgreSQL / MySQL connections, spreadsheet uploads (Excel becomes a data source), per-project binding |
| Data modelling | Schema snapshots, drift detection, relation canvas, reviewed cardinality, SQL models |
| AI modelling | Entity/field naming, role classification, metric/dimension drafts, alias and value suggestions |
| Metric governance | Atomic/derived metrics, default aggregation, formatting, semi-additive constraints, metric time axes |
| Query compilation | S2SQL, frozen join paths, parameterized SQL, read-only guard |
| Advanced queries | Set operations, period comparison, rolling ratios, group share |
| Ambiguity governance | Deterministic scope inference after generation, same-name confirmation cards, cross-fact-root metric clarification |
| Versioning | Revisions, ETags, immutable releases, semantic-index binding, switch to any published release |
| Query experience | Streaming stage events, result interpretation, textual S2SQL drill-down, visible and reversible default time window |
| Feedback loop | Vocabulary-gap inbox (six kinds), grouping by wording, one-click glossary entry |
| Runtime options | Assistant-level overrides: rows, time window, multi-turn rewriting, self-consistency, model, temperature |
| Quality | Two-mode Playground, golden suites, real-data quality reports |
| Observability | Fixed query timeline, failure records, redacted Markdown diagnostics |
| Deployment | Standalone web UI, Docker Compose, OpenAI-compatible model endpoints |

---

## Measured results

`scripts/product_accuracy_campaign.py` drives the authenticated product API: raw database → import → relationship confirmation → AI modelling → publish → load hidden questions after publishing → natural-language preview → compare rows with reference SQL.

The most recent measurement in `0.0.2` is the controlled experiment behind the final prompt catalog format: one dataset, 22 Chinese questions, each through the full query chain, with divergent questions rerun to n ≥ 4.

| | Per-entry dictionary (old) | Pipe table (new) |
|---|---:|---:|
| Mean prompt | 6161 chars | 4744 chars |
| End-to-end latency | 14.8 s | 11.7 s |
| Questions agreeing | 20 / 22 | 20 / 22 |

Both divergent questions favour the new format. "Which product sells best" is answered correctly 6/6, and "how many stores per city" is answered 4/4 within 6 seconds, while under the old format the first model call for that question timed out at 30 seconds and twice fell back to the rule parser, returning a list of store names: a plausible wrong answer.

The experiment also exposed one unfixed silent wrong answer. For "average sales amount per order" the model wrote `AVG(sales_amount)`, which is the mean over detail rows (49.37) rather than per order (98.66), and all six governance gates passed it. Grain misreading is not part of any current gate and is on the backlog.

This is a controlled experiment on one dataset, not a universal Text-to-SQL benchmark claim. The project tracks silent wrong answers separately from completion rate: a refusal or clarification is visible, a plausible wrong number is not. That `AVG` is the latter, and the gate for it is the next thing to add.

---

## Product workflow

```text
Connect datasource
  → import tables and confirm relations
  → generate an AI modelling draft
  → review metrics, dimensions, terms, and value dictionaries
  → run structural validation and real-data quality checks
  → test both Playgrounds
  → publish an immutable release
  → query from an agent, UI, or API
```

The workbench has four pages: data sources, semantic modelling, query validation, and query feedback. Inside semantic modelling the persistent navigation is entities and relations, business glossary, and catalog overview. Query scopes appear only in advanced diagnostics.

---

## Configuration and operations

### Compose options

| Variable | Default | Purpose |
|---|---|---|
| `KNOWFLOW_ANALYTICS_IMAGE` | `knowflowai/analytics:v0.0.2` | Analytics image to run |
| `KNOWFLOW_OSS_BIND_ADDRESS` | `127.0.0.1` | Host bind address |
| `KNOWFLOW_OSS_PORT` | `9395` | Published host port |
| `CATALOG_DB_PASSWORD` | `analytics` | Bundled catalog PostgreSQL password; must be URL-safe |
| `KNOWFLOW_OSS_ACCESS_PASSWORD` | empty | Standalone shared access password |

For remote deployment, explicitly bind to `0.0.0.0` and add an access password, HTTPS reverse proxy, and firewall:

```bash
KNOWFLOW_OSS_BIND_ADDRESS=0.0.0.0 \
KNOWFLOW_OSS_ACCESS_PASSWORD='replace-me' \
docker compose -f docker-compose.oss.yml up -d
```

`KNOWFLOW_OSS_ACCESS_PASSWORD` is a single-user shared secret, not multi-user authentication or RBAC.

### Daily commands

```bash
docker compose -f docker-compose.oss.yml logs -f analytics
docker compose -f docker-compose.oss.yml restart analytics
docker compose -f docker-compose.oss.yml down       # keep data
```

Two volumes store state: `catalog-data` for semantic resources, revisions, and releases; `analytics-data` for standalone settings. Neither stores a copy of the business database.

To reach PostgreSQL running on the Docker host:

```text
postgresql://user:password@host.docker.internal:5432/your_database
```

<details>
<summary>More runtime settings</summary>

| Variable | Default | Notes |
|---|---|---|
| `KNOWFLOW_OSS_CATALOG_DATABASE_URL` | required | Service-owned catalog database |
| `KNOWFLOW_OSS_DATA_DIR` | `./data` | Standalone settings directory |
| `KNOWFLOW_OSS_ACCESS_PASSWORD` | empty | Shared password in front of the UI |
| `KNOWFLOW_OSS_ALLOW_DEBUG_SQL` | `true` | Allow physical SQL in authorized diagnostics |

</details>

---

## Development from source

Requires Python 3.12+, Node 18+, [uv](https://docs.astral.sh/uv/), and PostgreSQL.

```bash
uv sync --python 3.12 --all-extras
(cd web && npm install)

createdb analytics_catalog
cp .env.oss.example .env
# edit KNOWFLOW_OSS_CATALOG_DATABASE_URL
set -a; source .env; set +a

# terminal 1
uv run knowflow-analytics-oss

# terminal 2
cd web && npm run dev
```

Build the standalone image locally:

```bash
docker build -t knowflowai/analytics:local .
KNOWFLOW_ANALYTICS_IMAGE=knowflowai/analytics:local \
docker compose -f docker-compose.oss.yml up -d
```

Tests:

```bash
uv run pytest
uv run ruff check src tests
(cd web && npx vitest run)
```

### Code map

```text
src/knowflow_analytics/
├── api.py             # projects, revisions, catalog, publish, query API
├── application.py     # use-case orchestration and product invariants
├── catalog/           # PostgreSQL persistence and releases
├── modeling/          # introspection, AI modelling, quality, scope compiler
├── query/             # mapper, parser, corrector, clarification, diagnostics
├── semantic/          # semantic index and S2SQL translator
├── execution/         # SQL guard and read-only PostgreSQL executor
├── gateways/          # chat, embedding, and knowledge gateways
└── oss/               # standalone configuration, access guard, static hosting
```

Before changing the query pipeline, read `tests/unit/test_query_stage_pipeline.py`, `tests/unit/test_textual_s2sql_pipeline.py`, and the relevant reviewed design documents. Natural-language and structured queries are separate contracts and must not be collapsed for implementation convenience.

---

## Data and security boundaries

- Physical SQL is parameterized and passes an AST allowlist, read-only transaction, and execution limits.
- Query result rows return directly to the caller and are not sent back to the model for summarization.
- Modelling can sample low-cardinality values for dictionaries and alias proposals; it can be disabled.
- Ordinary business answers do not expose internal scopes, datasets, semantic IDs, or physical SQL.
- Diagnostic exports redact passwords, API keys, connection credentials, and confirmation tokens.
- Standalone configuration files use mode `0600`; `.env`, `local.env`, and `data/` are excluded from Git and Docker build context.

Production deployments should still use a dedicated read-only database account, network isolation, TLS, and a proper identity layer outside the standalone shared-password shell.

---

## Current limits

- Data sources are PostgreSQL, MySQL, and uploaded spreadsheets. All five time grains, the period-comparison self join, and the share window match PostgreSQL row for row on real MySQL. No other dialect is supported yet.
- Standalone is single-user with an optional shared password; it does not provide multi-user RBAC or row/column policies.
- AI modelling runs synchronously inside the request deadline. Import large schemas in batches or increase the timeout.
- An entity reachable through two equally short join paths is excluded from that scope.
- An event/bridge table with neither an identifier nor a business measure cannot become a fact root.
- Multi-turn rewriting is enabled by default. It only reads the previous successful semantic query in the same conversation, release, and business scope, and it may complete wording without adding a grouping dimension the user never asked for.
- Confirmation memory was removed in `0.0.2`. A human choice applies to that query only; lasting fixes go through the business glossary.
- Result interpretation is disabled by default. It may only restate numbers present in the result and performs no calculation.

---

## Community and support

- **Website**: [www.knowflowchat.cn](https://www.knowflowchat.cn)
- **WeChat official account**: KnowFlow 企业知识库
- **Community group**: add WeChat `skycode007` and include “加群” in your request
- **Changelog**: [CHANGELOG.en.md](CHANGELOG.en.md)
- **Issue tracker**: [GitHub Issues](https://github.com/knowflow-ai/analytics/issues)
- **KnowFlow community**: [Community and support](https://github.com/knowflow-ai/KnowFlow#%E7%A4%BE%E5%8C%BA%E4%B8%8E%E6%94%AF%E6%8C%81)

---

## Contributing and license

Issues and pull requests are welcome. Changes to the mapper, parser, S2SQL, correctors, translator, routing, or execution must name the affected stage, preserve its input/output and failure contract, and add corresponding contract tests. Runtime branches for one dataset, business name, or benchmark wording are not accepted.

KnowFlow Analytics is licensed under the [Apache License 2.0](LICENSE).
