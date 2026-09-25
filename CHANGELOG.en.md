# Changelog

Notable changes to KnowFlow Analytics. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning note: while on `0.x`, semantic resource contracts, query stages, and failure behaviour may change between minor versions. Every such change is written down below with the behaviour it affects.

---

## [0.0.4] - 2026-09-25

Everything since `v0.0.3`. Image `knowflowai/analytics:v0.0.4` (`linux/amd64`, `linux/arm64`).

This release changes several behaviours (contract changes allowed while on `0.x`). Read "Changed" before upgrading.

### Added

- **Check against the data while modelling**: fields, relations and metrics can be checked in place (primary-identifier uniqueness and null rate, relation coverage and cardinality, metric samples, sample rows) instead of waiting for the pre-release quality report. Results are cached by the object's content: change the object and the key changes, while a rename invalidates nothing. Reading the cache never touches the business database; an actual measurement is rate-limited with an 8-second statement cap, and a measurement that could not be taken is not cached. New endpoints `GET/POST .../fact-checks`.
- **Column profiles are stored**: the profiles computed in the first step of AI modelling (uniqueness, null rate, value distribution) are kept, bound to the schema snapshot, and shown on the modelling page.
- **The data can overrule a primary identifier the AI asserts**: when the model calls a column a primary identifier but its measured uniqueness is below 0.95 on at least 100 rows, it becomes a foreign identifier and the measured numbers are written into the dispute note. Without a profile the rule-based verdict stands; no primary identifier is invented.
- **Metrics can be created by hand**: the form asks one question, how the metric is computed: aggregate one column (atomic) or combine other metrics (composite). No expression to write: pick a column, or pick two metrics and an operator.
- **Governed primitives `ENTITY_SHARE` and `RANK_OF`**: "share of entities meeting a condition" and "where does this entity rank" are expanded by the compiler from the stated intent (aggregate per entity before comparing with the threshold; rank over the whole set before picking the target) instead of relying on prompt conventions. Written wrong, both produce valid SQL that runs and returns a wrong number, and nothing in the SQL tells them apart, so they can only be primitives.
- **New compiler checks**: time grain may not be finer than the metric's declared `time_granularity` (`S2SQL_TIME_GRANULARITY_TOO_FINE`); metrics that declare `requires_explicit_time` must carry a time condition (`EXPLICIT_TIME_REQUIRED`); numeric thresholds must be numbers, e.g. not `'2万'` (`S2SQL_NON_NUMERIC_THRESHOLD`).
- **Hierarchy trees are part of the release**: `DimensionValueSpec.parent_value` records a value's parent, and the parent/child pairs are read once at publish time. Filtering by a parent (such as a parent account) reaches the child rows, and questions no longer read the customer's database for it.
- **Business glossary and value labels reach the prompt**: members declared in the glossary and business labels of governed values are given to the model; members the answer used without an exact match in the question (the model's own guesses) are reported with that answer.
- Diagnostics: when the model fails to produce SQL, each attempt's raw output (truncated) is recorded; stage detail is capped at 32 KB.
- Evaluation: test cases store the authoritative SQL and result columns, so few-shot examples are no longer reconstructed from the projection.

### Changed

- **The model declares the scope in `FROM`**, replacing 0.0.2's "try translating against each scope after generation and infer the scope from which one succeeds". The catalogue is grouped by scope and the model writes `FROM <scope>`; a query that translates in several scopes is no longer settled by grain convergence or a clarification card.
- **Primary identifiers are required only when needed** (as in Cube): a standalone table can be queried without one; it is required only when the table takes part in a one-to-many join and carries an additive measure. Every physical table gets a default count: tables with a primary identifier count keys (entities), tables without one count rows (`COUNT(*)`, and the description says it counts rows).
- **One-to-many fan-out is computed correctly instead of refused**: the amplifying join still runs, `SELECT DISTINCT(group-by dimensions + fact-root primary identifier)` collapses the duplicated rows, and the result is joined back to the fact root before aggregating. Without a primary identifier the query is still refused; many-to-many is never opened; frozen routes to entities that already have a safe path are unchanged.
- **Foreign identifiers can be grouped by**: only primary identifiers are hidden (grouping by one is grouping by row). Account numbers, store numbers and other foreign identifiers can be group-by dimensions; they still stay out of the value dictionary.
- **A model timeout or rejected candidates now end in a refusal** instead of falling back to the rule parser, whose answer was often a normal-looking wrong number.
- **Querying no longer asks the model to think by default**: a chain of thought does not change whether the structured output is correct, only how long it takes (10.5 s against 0.8 s measured); it can be turned on in configuration. The model named in the request now applies to SQL generation too, not only to result interpretation.
- SQL read timeout defaults to 60 seconds; each question has an overall time budget.
- Alias review no longer blocks publishing.
- The prompt's rule block is a fixed constant: rules the compiler already enforces were removed, and composition patterns became examples verified by the translator (4 → 11).
- The UI follows the Ant Design 5 specification; the primary colour is `#2b7de9`.

### Fixed

- "What share of the whole is this value": the denominator was filtered by the same dimension down to the numerator, always giving 1.
- `BETWEEN` predicates were rejected as a dropped condition.
- A zero-row aggregate was treated as success.
- Bare numbers and numeric literals in the question were taken for values of some dimension.
- Members matched by the mapper do not necessarily belong to the dataset; they are now filtered by membership.
- Aliases and parent accounts are honoured at query time.
- An identifier column whose name varies by scope made the whole generation catalogue be dropped.
- AI modelling: re-running a reviewed candidate failed outright; physical column names with spaces failed; alias batches timing out on slow models failed the whole run; the run was rejected by its own completeness check.
- Modelling page: renaming a plain field did not save; deleting a relation used by a route returned 500; the edge disappeared when a field it used became a plain field; plain fields showed as "to be confirmed"; "Apply" went blank; row-count metrics were flagged for missing a unit.
- Pre-release test questions reuse the semantic index instead of rebuilding it each time.
- The data source page header spans the available width.

---

## [0.0.3] - 2026-09-07

### Fixed

- The service now creates its catalog database when it is missing, the same way it
  already did for the upload database `analytics_uploads`. Previously
  `KNOWFLOW_ANALYTICS_AUTO_CREATE_SCHEMA` only ran `metadata.create_all`, which
  creates tables but cannot create a database, so a fresh deployment failed at the
  connection layer with `database "analytics_catalog" does not exist` and needed a
  manual `createdb` to start. When the account cannot create it, the startup error is
  now an explicit `CATALOG_DATABASE_UNAVAILABLE`; it never silently falls back to
  another database, and an existing database is left untouched.

---

## [0.0.2] - 2026-09-05

Everything since `v0.0.1`. Image `knowflowai/analytics:v0.0.2` (`linux/amd64`, `linux/arm64`).

### Added

- **Multiple data sources.** A data source is a first-class entity bound per project, so one project can read several databases.
- **MySQL dialect.** `sqlglot` stays the dialect authority; only three exceptions are hand-written, each verified against a real database: date truncation loses the date type, generated SQL must not contain `%` (the driver reads it as a parameter placeholder), and `DECIMAL` division truncates ratio precision. All five time grains, the period-comparison self join, and the share window match PostgreSQL row for row on real MySQL. `pymysql` ships with the image.
- **Spreadsheet uploads.** Excel becomes a data source and follows the same modelling and query chain as a database table. Multiple worksheets per import, append or replace, and delete are supported.
- **Streaming queries.** `POST /v1/analytics/query:stream` pushes stage events; the "understanding the question" event carries the semantic members the model recognised.
- **Result interpretation.** After the result returns, one extra model call turns it into a short paragraph. It may only cite numbers that appear literally in the result and may not compute totals, shares, or period comparisons of its own. Disabled by default; when enabled it never delays the result.
- **Assistant-level query options.** A single request may override row limits, the default time window, multi-turn rewriting, self-consistency, model, and temperature. Empty means follow the deployment default, so enabling this changes no existing deployment.
- **Default time window.** When a question states no time range, a configured recent-N-days window is added. Time the user did state is never touched; an added window is visible on the answer and can be cleared in one click.
- **Textual S2SQL continuation.** Drill-down and re-runs edit the textual S2SQL deterministically (change a filter value, add or drop a grouping dimension, switch the metric, change the time window) and keep the original time grain and period-comparison shape. Unsupported shapes are refused explicitly instead of silently degrading.
- **Query feedback.** Wording the user said and the system did not catch lands in one inbox, across six kinds: refused, clarified, inferred by the model, unknown value, liked, and disliked. Entries are grouped by wording, paginated, markable as handled, and can be pushed into the business glossary in one click. A dislike must carry a reason.
- **Release management.** The live release can be switched to any previously published release, and publish history is numbered.
- **Observability.** Every query stage carries its real elapsed time, and diagnostics expose each model and embedding call with its purpose, attempt, duration, and prompt size.

### Changed

- **Scope selection is now deterministic inference after generation.** Previously a scope was chosen from local evidence before the model answered; when that choice was wrong, the model was locked inside the wrong range and had to improvise, and all six governance gates passed. What the user received was a plausible wrong number. Now the final LLM sees the union of candidate scope members and writes business-name S2SQL, and the compiler tries a deterministic translation against each real scope: exactly one success binds, zero means the question crossed fact roots, several converge to the coarsest grain. The model proposes, the compiler decides. **Checking is easier than choosing.**
- **Clarification is now a fallback.** The business-object card is gone. Only same-name semantic elements and cross-fact-root metric phrases still ask.
- **Multi-turn rewriting is on by default**, behind two deterministic gates: no rewrite when the question already names a metric exactly, and a rewrite that introduces a grouping dimension the question never mentioned is discarded. A rewrite may complete the wording, never add a question.
- **The metric and dimension catalog in the final prompt** changed from per-entry dictionaries to a pipe table with one header row. Measured: prompt mean 6161 → 4744 characters, end to end 14.8s → 11.7s.
- **Model calls have per-purpose timeouts.** A timeout does not retry the same prompt at the transport layer; producing a different generation belongs to the parser retry chain. On retry the previous validation rejection (code, message, rejected SQL) is stated to the model as fact, without rewriting it.

### Fixed

- When the model could not express a condition it invented an always-false filter to stand in for it. Execution succeeded with 0 rows and the UI said "the query succeeded but returned no data", which the user reads as a false statement about their own business. Any `WHERE`/`HAVING` that constant-folds to FALSE is now refused.
- On 0 rows with a filter value outside the published values of that dimension, the value is named explicitly. A near-miss suggestion requires edit similarity ≥ 0.6, so an obvious typo gets a suggestion and an unrelated value gets no guess. The wording is "not among the published values", not "does not exist", because high-cardinality values may only be sampled.
- The whole "top N per group" class of questions.
- A word that happens to be a value of some dimension is no longer treated as a filter the user asked for.
- A statement with `GROUP BY` counts as an aggregate query, so aggregation written only in `ORDER BY` is no longer refused as a detail query.
- Period comparison: a governed aggregate written out explicitly is the same as a bare metric reference, and a metric that appears both bare in the projection and inside a ratio is wrapped with its governed aggregate.
- Time dimensions no longer take part in same-name ambiguity settlement. A time dimension enters the query because "by month" and "year over year" must land on a time axis, not because of how some noun reads. This previously caused an endless clarification loop.
- A `LIMIT` the query wrote itself is no longer misreported as a truncated result.
- Derived time columns and period-comparison columns without an alias are named after their business name instead of an internal column name.
- Deriving a draft from a published release dropped the review record of its semantic context.
- A data source may not be the service's own catalog database, which would model internal tables as business tables. Bare `postgresql://` is pinned to psycopg 3.

### Removed

- **Confirmation memory** in full: the memory table with its reads, writes, revocation, replay, and TTL, plus the pending alias-suggestion API.
- **Weak-recall confirmation cards and AI adjudication**: the weak-metric confirmation card, weak-metric AI adjudication, and business-intent and business-object AI adjudication. Keyword and vector weak hits still reach the final prompt as constraints, but no longer create cards or settlement obligations.
- **The "default data source" concept**, replaced by a one-time migration.

### Standalone edition

- The UI focuses on semantic modelling: projects, data sources, the modelling workbench, and settings. Natural-language trials now live in "pre-publish trial" inside the workbench, and the separate `/projects/:id/ask` page was removed.
- Settings keeps only the two model endpoints; business databases are always added under Database connections.
- The standalone shell and the bundled web build share one image.
- Environment variables were trimmed, and tuning options all have defaults.
- The service secret is persisted to `data/service_secret` with mode `0600`. Previously every encrypted connection string became undecryptable after a restart.

---

## [0.0.1] - 2026-08-31

First open-source release.

- Governed semantic catalog: models, relations, metrics, dimensions, terms, dimension values.
- AI-assisted modelling produces a reviewable candidate revision; only human confirmation publishes.
- Compiler-generated query scopes freezing a fact root, membership, and safe join paths.
- The LLM emits business-name S2SQL only; physical SQL is compiled deterministically by the translator, with a read-only AST allowlist, row limits, and execution timeouts in the guard.
- Two-mode Playground, golden suites, and real-data quality reports.
- Fixed-stage query timeline and redacted Markdown diagnostics export.
- Standalone Docker Compose deployment, image `knowflowai/analytics:v0.0.1` (`linux/amd64`, `linux/arm64`).

[Unreleased]: https://github.com/knowflow-ai/analytics/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/knowflow-ai/analytics/releases/tag/v0.0.2
[0.0.1]: https://github.com/knowflow-ai/analytics/releases/tag/v0.0.1
