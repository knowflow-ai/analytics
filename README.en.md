# KnowFlow Analytics

English | [简体中文](README.md)

**A governed semantic layer between your database and the LLM.**

A self-contained Python service: it owns semantic-model revisions, AI modelling proposals,
query scope compilation, scoped confirmation memory, governed S2SQL translation and read-only
PostgreSQL execution.

**The SQL that reaches your database is never written by the LLM.** The LLM only *chooses*
among published metrics and dimensions, in business names; physical tables,
columns, join paths, aggregate functions and parameters are compiled deterministically from
the semantic model by the translator. If the model references a name outside the release or
drops a confirmed filter value, the query is refused - not run.

Governance sits at both ends. **Upstream**, one AI modelling run produces a reviewable draft.
**Downstream**, a real-data quality report, regression suites and a two-mode playground catch
definition errors before they go live. Relation cardinality is required, fanout is detected
deterministically, and join paths freeze with the release. Ambiguous online questions are
not guessed: the user confirms once, and the choice is remembered only inside strict version
and context boundaries.

It ships with its own web UI. Apart from Docker, bring a business PostgreSQL reachable with
a read-only account and OpenAI-compatible chat and embedding endpoints; Compose starts the
service's catalog PostgreSQL for you.

```text
your PostgreSQL  →  import tables  →  confirm relations  →  AI modelling  →  publish
                                                                               ↓
                                                       "各地区的订单金额是多少？"  →  answer
```

![KnowFlow Analytics: datasource import, semantic modelling, human clarification, confirmation memory and one-click diagnostics](docs/screenshots/knowflow-analytics-walkthrough.gif)

---

## Table of contents

- [Why a semantic layer](#why-a-semantic-layer)
- [Product capabilities](#product-capabilities)
- [Quick start with Docker](#quick-start-with-docker)
- [Running from source](#running-from-source)
- [Configuration](#configuration)
- [Using the product](#using-the-product)
- [Architecture](#architecture)
- [Measured results](#measured-results)
- [Editions](#editions)
- [Development](#development)
- [Contributing and license](#contributing-and-license)
- [Current limits](#current-limits)
- [Roadmap](#roadmap)

---

## Why a semantic layer

Pointing an LLM straight at a database schema gets you plausible SQL and silently wrong
numbers. A column called `net_amount` with no comment becomes "实付金额" to the model, so a
user asking about "销售额" matches nothing — or worse, matches a row-count metric and gets a
confident answer that is off by three orders of magnitude.

This service puts a reviewed model in between:

- **Entities and relations** are confirmed by a human, including join cardinality, so a
  many-to-many join can never silently double a revenue total.
- **Metrics and dimensions** carry business names and aliases, so "销售额", "营收" and
  "GMV" all resolve to the same governed metric.
- **Query scopes** freeze what a question may touch: one fact root, explicit join paths,
  an explicit member list. They are **compiled deterministically** from the catalog you
  confirmed, not another concept for you to maintain. Adding a column to a table cannot
  quietly widen what is live.
- **Publishing is a gate.** A release is immutable; questions run against it, not against
  whatever the draft looks like right now.

---

## Product capabilities

### Modelling and governance

| Capability | What it does |
|---|---|
| Schema snapshots and drift detection | Imports selected PostgreSQL tables into a frozen schema snapshot; later drift is reported instead of silently absorbed |
| Relation canvas and cardinality review | Turns foreign keys into candidates, supports field-to-field manual relations, and requires a human cardinality decision |
| One-click AI modelling | Proposes business names, field roles, metrics, aliases, common dimension values, entity-name dimensions and default count metrics |
| Draft-only AI changes | Every AI change is itemized and reviewable; anything that would overwrite human-authored content starts unchecked |
| Governed metric definitions | Publishes atomic and derived metrics, default aggregation, display format, additivity and per-metric logical time axes; unsafe time aggregation is refused |
| Compiled query scopes | Deterministically freezes one fact root, safe join paths and the exact metrics and dimensions a question may use; scopes are diagnostics, not another form to maintain |

### Validation and release

| Capability | What it does |
|---|---|
| Real-data quality report | Checks identifier uniqueness and nulls, observed relation cardinality, fanout, metric samples and metric-to-dimension reachability with read-only queries |
| Two-mode playground | Structured mode bypasses natural-language parsing; natural-language mode exercises the complete production query state machine |
| Regression suites and reviewed few-shot examples | Save a correct trial as a regression case; explicitly reviewed cases can also guide similar questions in the same modelling version |
| Feedback loop | Keeps refused questions visible and aggregates repeated confirmations into review-only alias/Term evidence; neither path edits the active release |
| Immutable releases and rollback | Freezes the catalog, query projection and semantic index together, and can point the project back to the previous release without rewriting history |

### Querying

| Capability | What it does |
|---|---|
| One low-friction clarification | An ordinary query shows at most one blocking business card containing metric, dimension, value or analysis-object labels—never Scope, Dataset or internal IDs |
| Scoped confirmation memory | A successful explicit choice is reused only when actor, project, release, index, phrase, candidate set and exact context still match, and is disclosed as a remembered choice |
| Bounded AI adjudication | Optional AI may choose only among the current governed candidates. The default is `shadow`; abstention, invalid output or failed governance proof returns to the same human card |
| Traceable decision provenance | The response keeps human, memory, bounded-AI and final-LLM sources separate; the UI labels semantic-element choices and shows the chosen analysis object, with an explicit one-click switch |
| One-click diagnostics | Completed, failed and clarification results expose a fixed read-only timeline and a redacted Markdown report suitable for an Issue |
| Advanced governed S2SQL | Supports set operations, period-over-period/rolling ratios and group share without letting the LLM write physical SQL |

---

## Quick start with Docker

The compose file starts a PostgreSQL for the service's own catalog plus the service itself.
The database you want to *ask questions about* is configured later, in the UI.

```bash
git clone https://github.com/knowflow-ai/analytics.git
cd analytics
docker compose -f docker-compose.oss.yml up -d
```

Compose pulls `knowflowai/analytics:v0.0.1` for `linux/amd64` or `linux/arm64` and starts the
catalog PostgreSQL. Open <http://localhost:9395>. The service starts in an unconfigured state
and the UI walks you through three settings; there is no config file to edit first.

### Compose options

| Variable | Default | What it does |
|---|---|---|
| `KNOWFLOW_ANALYTICS_IMAGE` | `knowflowai/analytics:v0.0.1` | Analytics image to run; override only for local development or another explicit version |
| `KNOWFLOW_OSS_BIND_ADDRESS` | `127.0.0.1` | Host bind address. Use `0.0.0.0` only for a remote deployment and set an access password at the same time |
| `KNOWFLOW_OSS_PORT` | `9395` | Published host port |
| `CATALOG_DB_PASSWORD` | `analytics` | Password for the bundled catalog PostgreSQL. Keep it URL-safe (letters, digits, `-_.~`) — it is interpolated into a connection URL |
| `KNOWFLOW_OSS_ACCESS_PASSWORD` | *(empty)* | Shared password in front of the UI. Empty means no login screen; set it whenever the port is reachable from another machine |

```bash
KNOWFLOW_OSS_BIND_ADDRESS=0.0.0.0 \
KNOWFLOW_OSS_PORT=8080 \
KNOWFLOW_OSS_ACCESS_PASSWORD='choose-something' \
docker compose -f docker-compose.oss.yml up -d
```

`KNOWFLOW_OSS_ACCESS_PASSWORD` is a single-user shared secret, not an identity or policy
system. Put remote deployments behind HTTPS and a firewall, and use a dedicated read-only
account for the business datasource. Never expose an unauthenticated `0.0.0.0` port to the
public Internet.

### Everyday operations

```bash
docker compose -f docker-compose.oss.yml logs -f analytics   # follow logs
docker compose -f docker-compose.oss.yml restart analytics   # restart the service
docker compose -f docker-compose.oss.yml down                # stop, keep data
docker compose -f docker-compose.oss.yml down -v             # stop and delete all data
```

Two named volumes hold state: `catalog-data` (semantic models, revisions, releases) and
`analytics-data` (your settings file). Neither holds a copy of your business data.

### Connecting to a database on the host

Inside the container, `127.0.0.1` is the container itself. To reach PostgreSQL running on
your laptop, use `host.docker.internal`:

```text
postgresql://user:password@host.docker.internal:5432/your_database
```

### Building the image yourself

```bash
docker build -f Dockerfile.oss -t knowflowai/analytics:local .
KNOWFLOW_ANALYTICS_IMAGE=knowflowai/analytics:local \
docker compose -f docker-compose.oss.yml up -d
```

The Dockerfile is two stages: Node builds the web bundle, Python installs the package and
copies the bundle in. The final image runs as a non-root user and serves both the API and
the UI on one port.

---

## Running from source

Use this when you want to change the code. You need Python 3.12+, Node 18+,
[uv](https://docs.astral.sh/uv/), and a PostgreSQL you can create a database in.

### 1. Install dependencies

```bash
uv sync --python 3.12 --all-extras
(cd web && npm install)
```

### 2. Create the catalog database

The service stores semantic models in its own database. **It must not be the database you
want to query** — the service's own `analytics_*` tables would show up as business tables to
model. Saving a datasource that points at the catalog database is rejected in the UI.

```bash
createdb analytics_catalog
# or: psql -c 'CREATE DATABASE analytics_catalog;'
```

Tables are created automatically on first start.

### 3. Configure and run

```bash
cp .env.oss.example .env
$EDITOR .env          # set KNOWFLOW_OSS_CATALOG_DATABASE_URL
set -a; source .env; set +a

(cd web && npm run build)      # build the UI once
uv run knowflow-analytics-oss  # http://localhost:9395
```

### Frontend development

For UI work, run Vite instead of rebuilding. It proxies `/api` and `/v1` to the Python
service, so run both:

```bash
# terminal 1
uv run knowflow-analytics-oss
# terminal 2
cd web && npm run dev          # http://localhost:5273, hot reload
```

### Project layout

```text
knowflow-analytics/
├── src/knowflow_analytics/
│   ├── api.py               # HTTP surface: projects, revisions, catalog, publish, query
│   ├── application.py       # use cases; the only place that orchestrates the core
│   ├── contracts.py         # published semantic release: models, metrics, dimensions
│   ├── catalog/             # revision + release persistence (PostgreSQL)
│   ├── modeling/            # schema introspection, AI modelling, query scope compilation
│   │   ├── catalog_contracts.py   # typed catalog DTOs, round-tripped without loss
│   │   └── catalog_compiler.py    # catalog → deterministic query projection
│   ├── query/               # mapper → parser → corrector → orchestrator
│   ├── semantic/            # S2SQL translation and the semantic index
│   ├── execution/           # read-only PostgreSQL executor and SQL guard
│   ├── gateways/            # chat / embedding model clients
│   └── oss/                 # standalone shell: settings, HTTP host, model gateways
├── web/                     # the bundled UI (Vite + React)
├── tests/unit/              # no database required
├── tests/integration/       # needs KNOWFLOW_ANALYTICS_TEST_DATABASE_URL
└── docker-compose.oss.yml
```

### Where to start reading

| I want to… | Start at |
|---|---|
| Add an HTTP endpoint | `api.py`, then the matching method on `application.py` |
| Change how a question becomes SQL | `query/orchestrator.py` → `query/parser.py` → `semantic/s2sql_translator.py` |
| Change what the AI proposes during modelling | `modeling/ai_modeller.py` and `modeling/prompts.py` |
| Change the catalog shape | `modeling/catalog_contracts.py`, then `catalog_compiler.py` |
| Support a different model provider | `oss/gateways.py` (OpenAI-compatible today) |

---

## Configuration

Only the catalog database is configured through the environment. The datasource and the
model endpoints are set in the UI and stored in `$KNOWFLOW_OSS_DATA_DIR/config.json` with
mode `0600`, so API keys never sit in your shell history or a compose file.

| Variable | Default | Notes |
|---|---|---|
| `KNOWFLOW_OSS_CATALOG_DATABASE_URL` | *(required)* | The service's own database. A bare `postgresql://` URL is upgraded to the psycopg3 driver automatically |
| `KNOWFLOW_OSS_DATA_DIR` | `./data` | Where `config.json` lives |
| `KNOWFLOW_OSS_HOST` | `127.0.0.1` | Loopback by default; the Docker image sets `0.0.0.0` |
| `KNOWFLOW_OSS_PORT` | `9395` | |
| `KNOWFLOW_OSS_ACCESS_PASSWORD` | *(empty)* | Shared password for the UI |
| `KNOWFLOW_OSS_WEB_DIST` | `./web/dist` | Built UI to serve |
| `KNOWFLOW_OSS_MODELING_MAX_CONCURRENCY` | `3` | Parallel model calls during AI modelling. Lower it to `1` if your provider rate-limits |
| `KNOWFLOW_OSS_MODEL_TIMEOUT_SECONDS` | `120` | One modelling call against a large model can take a minute |
| `KNOWFLOW_OSS_MODELING_SAMPLE_VALUES` | `true` | Whether modelling samples low-cardinality values. Disable for stricter data-egress policy at the cost of fewer dimension-value and alias suggestions |
| `KNOWFLOW_OSS_MULTI_TURN_ENABLED` | `false` | Opt-in follow-up question rewriting |
| `KNOWFLOW_OSS_SELF_CONSISTENCY_NUMBER` | `1` | Independent S2SQL generations to majority-vote; `1` disables it, larger values multiply model cost |
| `KNOWFLOW_OSS_WEAK_METRIC_ADJUDICATION_MODE` | `shadow` | Shadow mode evaluates but still asks the user; explicitly switch to `auto` after calibration; `off` skips AI |
| `KNOWFLOW_OSS_SEMANTIC_INTENT_ADJUDICATION_MODE` | `shadow` | Independent rollout for cross-type and multi-metric-phrase ambiguity; does not inherit weak-metric `auto` |
| `KNOWFLOW_OSS_ANALYSIS_OBJECT_ADJUDICATION_MODE` | `shadow` | Independent rollout for no-metric, multi-business-grain ambiguity; the deterministic resolver still owns the internal scope |
| `KNOWFLOW_OSS_CONFIRMATION_MEMORY_TTL_SECONDS` | `2592000` | TTL for explicit, version-bound user choices; actor, project, release, index, candidates or context changes prevent reuse |
| `KNOWFLOW_OSS_ALLOW_DEBUG_SQL` | `true` | Allow physical SQL in draft previews and authorized diagnostics; ordinary release answers still hide internal scope and IDs |

### Models

Any OpenAI-compatible endpoint works — hosted APIs, or a local vLLM/Ollama with an empty
API key. You need two:

- **Chat model** — used for AI modelling, weak-metric business adjudication, and for turning a question into semantic SQL.
  Use a capable model here; this is the single biggest lever on answer quality. Reasoning
  models are fine, their thinking output is stripped.
- **Embedding model** — used to build the semantic index that matches question wording to
  governed names. Changing it later requires re-publishing.

Each has a **Test** button that does one real round trip before you save.

---

## Using the product

### 1. Settings

Connect your PostgreSQL (use a dedicated read-only account), then the two model endpoints.
Everything is validated before it is stored.

### 2. Create a project and import tables

A project is one semantic model. Pick a schema, tick the tables you want, import. The
service snapshots their structure — later schema drift is detected against that snapshot
rather than silently absorbed.

![Pick a schema and import tables](docs/screenshots/01-table-import.png)

### 3. Relation canvas

Imported tables appear as entities. Foreign keys become relation candidates that still need
one human decision: **cardinality**. This is not busywork — a relation marked many-to-one
when the data is actually many-to-many will double every sum that crosses it. Drag between
identifier fields to add a relation the database does not declare.

You can also edit entity names, field roles (identifier / dimension / time / measure),
and the dimensions and metrics each entity owns.

![Relation canvas: foreign keys become candidates awaiting a cardinality decision](docs/screenshots/02-relation-canvas.png)

### 4. AI modelling

One run does four things: names tables and fields in business terms, classifies each field's
role and generates metrics, generates aliases for metrics, dimensions and common dimension
values, and derives entity-name dimensions plus default count metrics, then compiles safe query scopes.

The result is a **draft**. Every suggestion is listed with what it would change, and you
untick anything you disagree with before adopting. Suggestions that would overwrite
something a human already wrote are unticked by default.

![AI modelling progresses table by table](docs/screenshots/03-ai-modeling-progress.png)

![Reviewing the proposal: every suggestion carries its role, description and reasoning](docs/screenshots/04-proposal-review.png)

### 5. Catalog and business glossary

The permanent navigation has exactly three entries: **catalog overview, entities and
relations, business glossary**. Those are what you maintain — there is no "topic" to
create, edit or pick.

Query scopes are **compiled deterministically** from the catalog: every entity that owns a
business metric gets one, whose members are its metrics plus the dimensions reachable over
safe relation paths. Scopes show up in advanced diagnostics for review; they are not a form
you fill in.

![Catalog overview: entities, metrics, dimensions and the business glossary](docs/screenshots/05-analysis-topics.png)

### 6. Validate and publish

Validation checks relation cardinality, primary identifiers, metric reachability and scope
paths, and reports blocking problems with the action that fixes each one. The quality report
also queries the real database read-only to verify identifier uniqueness, observed relation
cardinality, metric samples and fanout evidence; it reports evidence and never edits the model.

Before publishing, try questions two ways:

- **Natural language** — the full pipeline. If this fails but the structured trial works,
  the problem is aliases or mapping.
- **Structured** — pick metrics, dimensions and filters directly, skipping natural-language
  parsing. If this fails, the problem is the model or the SQL translation.

After a natural-language trial returns the right answer, save it as an evaluation case. Run
the suite after later model changes to catch regressions. Evaluation membership and few-shot
eligibility are separate: only a case explicitly reviewed and enabled for memory can guide
similar questions.

![Structured trial: pick metrics and dimensions, execute, expand the semantic and physical SQL](docs/screenshots/06-structured-playground.png)

### 7. Ask

Ask the published release. Ordinary answers contain business metrics, dimensions, filters,
results and the source of each business interpretation. They do not leak internal Scope,
Dataset or semantic IDs, or physical SQL. Candidate-revision trials can expand S2SQL and
physical SQL; every turn can also open the fixed diagnostic timeline and export a redacted
report when the deployment permits it.

If wording is ambiguous, the service shows at most one business confirmation card. The
choice resumes from the original question and version; it is saved only after the query
executes successfully. A later exact-context match is labelled as a remembered confirmation
and can still be switched explicitly.

---

## Architecture

```text
browser (web/)
  ↓ HTTP
oss shell        settings, single-user auth, static hosting  (src/knowflow_analytics/oss/)
  ↓ in-process
core API         projects · revisions · catalog · publish · query   (api.py)
  ↓
application      use cases and invariants                     (application.py)
  ↓                        ↓                        ↓
catalog store       modelling + AI            query pipeline
(PostgreSQL)        (model gateways)          mapper → parser → corrector
                                              → translator → guard → executor
                                                        ↓ read-only SQL
                                                   your PostgreSQL
```

### The LLM never writes the SQL that runs

The diagnostics view shows the LLM's output and it looks like SQL, but it is **semantic
SQL**: a list of choices expressed only in business names. It contains no physical table,
no physical column, no join, no parameter - it has no vocabulary for them. The SQL that
touches the database is compiled:

```text
semantic SQL (business names)   SELECT AVG("二套房首付比例") FROM "城市分析" WHERE "城市名称"='南京'
   ↓ symbol binding              names/aliases → published semantic IDs (unknown name ⇒ refused)
   ↓ translator                  catalog fixes columns and aggregates; RouteSpec fixes joins (frozen at publish)
physical SQL (parameterized)    SELECT AVG("m0"."二套房首付比例") FROM "bench_6"."城市" AS "m0" WHERE "m0"."名称" = :p0
   ↓ guard                       AST allowlist + read-only transaction + timeouts
```

A table, column or join the LLM invents cannot reach the database: the word fails symbol
resolution and the query is refused before the translator. An exact value the mapper
confirmed (such as 南京) that goes missing from the LLM output is refused the same way.
Both paths share this compiler, and the structured playground involves **no LLM at all** -
which is why it can split "modelling is wrong" from "mapping is wrong".

Design rules the code holds to:

- **Semantic resources are versioned and atomic.** Every write carries the revision ETag and
  the schema-snapshot hash; a stale form cannot overwrite a newer model.
- **A release is immutable.** Publishing freezes the catalog, the query projection and the
  semantic index together.
- **Rollback only moves the active pointer.** Historical releases stay immutable; rollback
  neither rewrites nor rebuilds them.
- **Execution is read-only and guarded.** Generated SQL is parameterized and checked before
  it reaches the database.
- **Query result rows never reach a model.** The answer pipeline has no "feed the rows
  back for interpretation" step. The only data values the chat model sees are the
  published dimension-value dictionary - sampled during modelling, human-reviewed,
  frozen with the release.

### What happens after a human clarification

Confirmation memory removes a narrow, repeatable annoyance: if one person already said
that “sales” means the governed “net revenue” metric here, the service should not ask again
while the release and question context are unchanged. One click must not become a permanent,
cross-version business fact either.

```mermaid
flowchart LR
    Q["Current governed candidates"] --> U{"Unique?"}
    U -->|yes| P["S2SQL pipeline"]
    U -->|no| H["One business card<br/>signed opaque token"]
    H --> C["Explicit user choice"]
    C --> S{"Final S2SQL actually uses it?"}
    S -->|no| H
    S -->|yes| E["Read-only execution succeeds"]
    E --> M["Write expiring confirmation memory"]
    M --> R["Exact-context reuse<br/>shown as remembered"]
    R --> S
    M -. "version / candidates / context changed" .-> H
```

The memory lookup happens only after the current candidate set exists, so history cannot
invent a metric, dimension or fact grain. A memory is written only after final settlement,
all governance checks and database execution succeed. It is bound to the actor, project,
release, spec hash, semantic index, normalized phrase, candidate-set hash and exact context;
expiry, conflict, revocation or a changed candidate makes the memory layer abstain. The
default `shadow` path asks again; a memory-store read failure additionally disables AI auto
adjudication for that turn and falls straight back to the human card. AI decisions never
write long-term memory. Explicit card choices and explicit result-chip switches do.

The response keeps `human`, `memory`, `ai` and `final_llm` provenance distinct. The UI labels
current semantic-element choices as confirmed, remembered or automatically understood;
analysis-object chips show which object is in use, while advanced diagnostics retain the
complete source.

Continuation uses a short-lived HMAC-signed opaque token bound to the original question,
actor, allowed dataset set, release versions, displayed candidates and TTL. The client
returns it unchanged. Tampering, expiry, replay in another question or actor, and selecting
an undisplayed option all fail closed. When semantic meaning and business grain must be
confirmed together, one option carries the complete business choice without exposing an
internal Scope.

Users can list active records with
`GET /v1/analytics/projects/{project_id}/confirmation-memories` and revoke one with `DELETE`
on the same path plus `/{memory_id}`. Repeated choices are aggregated by the project's
`GET /confirmation-suggestions` endpoint into review evidence for an alias or Term; that
evidence still has to enter a Candidate Revision and pass publishing. It never edits the
active release directly.

### AI may help disambiguate, but it cannot authorize execution

Weak-metric, cross-type/multi-phrase, and no-metric business-grain adjudication have
independent `off | shadow | auto` switches. All default to `shadow`: the decision is recorded
for calibration while the user still gets the human card.

Even in `auto`, the model sees only governed business labels, aliases and descriptions plus
local opaque candidate keys. It neither sees nor outputs Scope/Dataset/root IDs, physical
tables or columns, joins, S2SQL, embedding scores or score gaps. A returned choice must still
survive deterministic scope resolution, frozen routes, fanout and version gates, and final
settlement must prove that authoritative S2SQL actually used the selected member. Abstention,
invalid output, stale candidates or a non-unique proof falls back to human confirmation.

### One-click diagnostics never participates in querying

Every completed, failed or clarification turn can open a fixed timeline covering modelling
context, `PRECHECK`, candidate discovery, final parsing, correction, routing, translation,
SQL guard, execution and post-processing. It can export a Markdown report for an Issue.

The recorder runs off the response path: it does not retry the question or repair intent.
Exports are isolated by actor, project, permission snapshot and TTL. Authorization, cookies,
passwords, connection credentials, API keys and signed confirmation tokens are redacted;
physical SQL remains subject to `ALLOW_DEBUG_SQL`, and production does not retain result-row
samples by default. See [`docs/one-click-query-diagnostics.md`](docs/one-click-query-diagnostics.md).

---

## Measured results

The harness is `scripts/product_accuracy_campaign.py`. It drives **the same authenticated
APIs the browser uses** — import tables, confirm relations, run AI modelling, publish — and
only **after publishing** does it load the hidden question set. Reference answers come from
hand-written SQL run directly against the database and are compared row by row (numeric
normalisation, unordered set comparison).

Three Chinese schemas, 12 questions each, covering single-table grouping, cross-table
grouping, Top N, dimension-value filters, numeric filters and entity counts.

| Schema set | Shape | Correct | **Silent wrong** | Notes |
|---|---|---|---|---|
| E-commerce Double 11 | 6 tables | **12 / 12** | **0** | Two metrics literally named "交易额" are separated by qualified aliases |
| City · Library | 3 tables | **11 / 12** | **0** | The one failure is an HTTP transport blip, not a semantic error |
| Music (new holdout) | 6 tables · 8 FKs · 3 PKs | 9 / 12 | 0 (see below) | Contains multi-path entity reachability and a PK-less event table |

**The silent-wrong column matters most.** A wrong refusal is visible to the user; a
plausible-looking wrong number is not. The first two suites are at zero.

The music suite was added at the end as a holdout, deliberately picking structures the
other two lacked. The three misses break down as:

- **q07 / q08 ("cover rating per singer")** — the system aggregates with `AVG`, the
  reference SQL uses `SUM`. The join path, the grouping entity and every other value match;
  the disagreement is purely the governed aggregation. The modelling record carries
  `high_impact=true` and the rationale "looks like a ratio/unit price, summing across rows
  is meaningless" — for a rating column, **`AVG` is arguably more correct than the `SUM` the
  question author wrote**. Decisions like this are meant to be adjudicated by a human; the
  harness auto-accepts everything for reproducibility, so it scores as a failure.
- **q11 ("awards per edition")** — the awards table has neither a primary key nor a business
  measure, so under the current rule it never becomes a fact root and the whole table is
  unqueryable. This is a **known limit**, surfaced before publishing by the
  `MODEL_OUTSIDE_EVERY_QUERY_SCOPE` diagnostic.

The same suite scored 6/12 before the fixes, with every path-dependent question refused.
Changing the path-ambiguity rule from "any other path exists" to "another path of the same
shortest length exists" made the cross-table grouping questions pass.

### How to read these numbers

- **Every run re-runs AI modelling**, so the results include modelling variance. The same
  column's aggregation can flip between runs (`participating merchants` was judged `AVG` in
  5 of 7 modelling runs and `SUM` in 2). The system does **not** pretend to be sure: both
  runs flagged `high_impact`, and the disagreeing one recorded "rule and model disagree,
  awaiting adjudication".
- **The harness measures unattended accuracy**, which is stricter than the product promises.
  The product contract requires AI drafts to be **reviewed before adoption**; the harness
  reports `ai_override_count: 0`.
- Suites and artefacts live under `tmp/product_accuracy_*/`; each report carries the semantic
  SQL, the physical SQL and result hashes per question, so the numbers can be re-checked.

---

## Editions

The boundary is simple: **the semantic layer is fully open source** - modelling,
governance, evaluation suites, publishing and release lifecycle, the query pipeline and the
standalone UI. Both deployments run the same business logic behind the same resource API;
the differences come from where they are deployed - how models are configured, where
multi-user and policies come from, and whether there are host documents to cite. The same
Python package serves two deployments:

| | Standalone (this README) | Embedded in KnowFlow |
|---|---|---|
| Entry point | `knowflow-analytics-oss` (`knowflow_analytics.oss.server`) | `knowflow_analytics.server:create_app` |
| Browser → service | bundled UI in `web/`, single local user | host BFF with signed actor/project context |
| Chat / embedding models | any OpenAI-compatible endpoint, set in the UI | tenant-managed model gateways |
| Knowledge-base evidence | none | host documents |
| Publish gate | validation only | validation + evaluation suite + quality report |

Everything under `src/knowflow_analytics/oss/` is the standalone shell. It composes the core
through the same `AnalyticsApplication` and `create_api` entry points and never modifies core
modules, so both deployments exercise identical code paths.

---

## Development

```bash
uv run pytest                                    # unit tests, no database needed
uv run ruff check src tests                      # lint
uv run ruff format src tests                     # format
(cd web && npx tsc -b && npm run build)          # typecheck and build the UI
(cd web && npx vitest run)                       # UI unit tests
```

Integration tests need a real PostgreSQL:

```bash
KNOWFLOW_ANALYTICS_TEST_DATABASE_URL='postgresql+psycopg://...' uv run pytest tests/integration
```

Conventions worth knowing before you send a patch:

- Contracts are `extra="forbid"` on purpose. A catalog round-trips through the API and the
  database, and a silently dropped key would corrupt a published release. If you retire a
  field, add a `mode="before"` validator that drops the old key so stored releases still
  load — see `SemanticCatalog.drop_retired_keys`.
- The query pipeline never repairs semantic intent in a diagnostic stage. Precheck, routing,
  guard and post-processing observe; they do not choose.
- Anything the AI produces lands as a reviewable draft, never directly in a revision.
- Behaviour is frozen by contract tests under `tests/`. Change code and the corresponding
  contract together; do not make a silent pipeline change look like a bug fix.

---

## Contributing and license

Issues and pull requests are welcome. This project cares less about two final answers looking
similar than about preserving the staged contract that produced them. For changes to mapping,
parsing, S2SQL, correction, translation, routing, execution or evaluation, describe in the PR:

1. the exact stage, its input/output contract and whether failure behaviour changes;
2. the contract test proving neither query pipeline was merged, skipped or reordered;
3. whether runtime code branches on a dataset, business term or benchmark wording—such
   patches are not accepted;
4. the targeted checks you ran. Documentation changes should at least pass
   `git diff --check`; Python changes should run relevant `pytest` and `ruff`; frontend
   changes should run the relevant Vitest and lint checks.

Open an Issue before intentionally changing a frozen query contract. Existing stage and
failure behaviour is defined by the contract tests under `tests/` and the reviewed designs
under `docs/`.

KnowFlow Analytics is released under the [Apache License 2.0](LICENSE). Refer to this
module's license text for the applicable use and distribution terms.

---

## Current limits

- PostgreSQL is the only supported datasource.
- Automatic relation confirmation covers database foreign keys with clear cardinality
  evidence. Relations the database does not declare need a human decision.
- Governed S2SQL supports `UNION`/`UNION ALL`/`INTERSECT`/`EXCEPT`, period comparison
  (`RATIO_OVER` / `RATIO_ROLL`) and group share (`RATIO_TO_TOTAL`). Period comparison needs
  one governed time grouping.
- Multi-turn rewriting is opt-in and only reads the previous successful turn in the same
  conversation, release and query scope.
- Confirmation memory supports provenance, API listing and revocation. The standalone UI
  does not yet have a dedicated memory-management screen; a candidate, context or version
  change automatically disables reuse.
- All three AI adjudication gates default to `shadow`. Calibrate them on your own real
  questions before enabling `auto`; it is not a global “make answers better” switch.
- AI modelling runs synchronously behind the request deadline. Before opening very large
  multi-schema scopes, expect to raise `KNOWFLOW_OSS_MODEL_TIMEOUT_SECONDS` or import in
  batches.
- **When an entity is reachable over two equally short paths** (one table carrying two
  foreign keys to the same entity), that entity leaves the scope. Role-playing dimensions
  would be required, and the translator keys table aliases by model, so one table can only
  have one alias per query. A strictly longer detour is unaffected.
- **A pure event/bridge table with neither a primary identifier nor a business measure never
  becomes a fact root**, so none of its fields are queryable. A diagnostic points this out,
  but there is no `COUNT(*)` fallback today.
- The standalone edition is single-user with an optional shared password; multi-user and
  row/column-level policies belong to the commercial edition.

---

## Roadmap

The limits above are today; this is what comes next. Vote or claim an item in the issues.

**Near term**

- [ ] **Conversational modelling.** Edit the model in plain language from the proposal
  review ("首付比例 is a measure, aggregate with AVG"). The conversation still produces
  itemized draft suggestions that go through the same adoption review and structural
  validation - the AI writes drafts, never the model.
- [ ] **YAML import/export for the semantic model.** Export the catalog to YAML for git
  review and cross-environment migration; import creates a new Candidate Revision that
  passes full validation before adoption. **YAML is a transport format, not a second
  authoritative model** - the resource API and immutable releases stay the single source
  of truth, otherwise two sources drift and every existing gate (ETags, freezing,
  fail-closed checks) is bypassed.
- [ ] **Confirmation-memory governance UI.** The backend already lists and revokes memories
  and aggregates repeated choices into review suggestions. Add a screen where users can
  manage their choices and modellers can review alias/Term evidence.

**Mid term**

- [ ] **More datasources - MySQL first** (PostgreSQL only today).
- [ ] **Data-profile evidence in field classification.** Value-shape features (all values
  in [0,1] ⇒ ratio, integers in 1900-2100 ⇒ year) are language-independent evidence, so
  small tables and comment-free schemas rely less on column-name guessing.

**Where open source ends and the commercial edition begins**

This repository open-sources the **complete semantic layer** - modelling, governance
contracts, evaluation suites, the pre-publish quality report, the release lifecycle, the
query pipeline and the standalone UI. It is not a crippled core: for semantic modelling the
two editions are the same code behind the same resource API, with no feature flag holding
capabilities back from open source.

The only differences are the ones the deployment shape forces:

- The standalone edition **configures its own chat/embedding models**; the embedded one
  reuses the host's tenant model gateways.
- Evaluation suites, the pre-publish quality report, releases and rollback are available in
  both editions - they are the same mounted API. Only the **publish gate defaults** differ:
  standalone publishes on structural validation alone, while the embedded edition requires
  evaluation and the quality report to pass first
  (`require_evaluation_for_publish` / `require_quality_report_for_publish`).
- The standalone edition is **single-user with an optional shared password**. Multi-user and
  row/column-level policies come from the host KnowFlow's RBAC rather than from the semantic
  layer, and are not on the standalone roadmap - please open an issue before sending a PR in
  that direction.
- Knowledge-base evidence needs the host's document store; standalone has nothing to cite.
- The commercial edition will add a **question-answering entry point for business users**
  (embedded in KnowFlow's chat). That is an entry point; the semantic layer underneath is
  still the one in this repository.
