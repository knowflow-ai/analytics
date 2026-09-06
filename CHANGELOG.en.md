# Changelog

Notable changes to KnowFlow Analytics. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning note: while on `0.x`, semantic resource contracts, query stages, and failure behaviour may change between minor versions. Every such change is written down below with the behaviour it affects.

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
