<p align="center">
  <img src="docs/assets/readme-banner.png?v=jev4pg" width="1000" alt="jev4pg: Natural language. Semantic SQL. Pangolin mascot with three parallel data paths." />
</p>

<h1 align="center">jev4pg</h1>

<p align="center">
  <strong>Natural language to SQL and semantic operators for PostgreSQL</strong>
</p>

<p align="center">
  <a href="https://github.com/Sheltercosmo/jev4pg/releases/tag/v0.7.0"><img src="https://img.shields.io/badge/release-0.7.0-18181b?style=flat-square&amp;labelColor=52525b" alt="Release 0.7.0" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-18181b?style=flat-square&amp;labelColor=52525b" alt="Apache 2.0 license" /></a>
</p>

<p align="center">
  <a href="https://jev4pg.com">Website</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#use-an-existing-postgresql-database">Existing PostgreSQL</a> ·
  <a href="#why-choose-jev4pg">Advantages</a> ·
  <a href="#bird-challenging-100-questions-11-databases">Benchmark</a> ·
  <a href="#what-you-can-build">What you can build</a> ·
  <a href="https://jev4pg.com/guide/">Project guide</a> ·
  <a href="docs/INSTALLATION.md">Installation</a> ·
  <a href="docs/USER_GUIDE.md">User guide</a> ·
  <a href="docs/zh/USER_GUIDE.md">简体中文</a> ·
  <a href="docs/JEV_FUNCTION_REFERENCE.md">Function reference</a> ·
  <a href="docs/README.md">Documentation</a>
</p>

jev4pg is a carefully designed JEV harness, brings semantic intelligence to PostgreSQL with a comprehensive toolkit of **41 JEV operators** for filtering, extraction, ranking, matching and verification. Build semantic search, document workflows and analyst tools on your existing data, with natural-language queries in English and Simplified Chinese.

**Explainable embeddings** represent data through named questions and answer probabilities, making similarity inspectable. The native preview combines these with **parallel execution and input deduplication**: independent work runs concurrently, while repeated values with the same context and questions share judgments, preserving every source row.

**Evidence caching** retains raw observations for compatible queries and threshold changes, with source and evaluator identity checks. **Controlled retries and reviewable fallbacks** keep errors, uncertainty and skipped work explicit, and preserve legal SQL proposals for correction. Reviewed definitions and corrections build a **self-developing semantic layer** your team can reuse across reports and applications. PostgreSQL handles joins, windows and exact arithmetic.

See it in action at [jev4pg.com](https://jev4pg.com), then build with the workspace, HTTP API or PostgreSQL interfaces.

<p align="center">
  <img src="docs/assets/product-tour.gif?v=b87ec970" width="800" alt="Animated product tour: natural-language SQL, semantic filtering, text extraction, parallel JEV stages and probability embeddings." />
</p>

## Quick start

Requires **Python 3.11+** and **Docker with Compose v2**. This starts PostgreSQL, the web workspace and its workers. Configure a [JEV provider](docs/PROVIDERS.md) for natural-language and semantic queries; ordinary SQL needs no model key.

```bash
git clone --branch v0.7.0 https://github.com/Sheltercosmo/jev4pg.git
cd jev4pg
python deploy/configure.py
docker compose build
docker compose up -d --wait
python deploy/configure.py --show-token
```

The configuration prompt accepts a TypeSafe key; leave it empty if you will configure another provider. Open [English](http://127.0.0.1:8000/ask/en) or [简体中文](http://127.0.0.1:8000/ask/zh) and connect with the workspace token printed by the last command. Keep that token private.

### Run your first query

Save this as `deliveries.csv`. In the workspace, choose **Import CSV**, name the dataset `deliveries`, review the column types and create it. Use integer `id`, text `supplier` and numeric `quantity`; the empty quantity is NULL.

```csv
id,supplier,quantity
1,Aster,12
2,Aster,18
3,Birch,30
4,Cedar,8
5,Cedar,
6,Birch,6
```

Select `deliveries`, choose **SQL** mode, and run:

```sql
SELECT supplier, SUM(quantity) AS total
FROM deliveries
GROUP BY supplier
ORDER BY total DESC;
```

Expected result: **Birch 36 · Aster 30 · Cedar 8**. Then switch to **Natural language · JEV**, ask “For each supplier, show the total quantity delivered, largest total first,” and choose **Preview plan** to inspect the generated SQL before running it. Hybrid mode additionally needs [LLM configuration](docs/HYBRID_QUERY.md#configuration).

[More query examples](docs/NL2SQL_EXAMPLES.md) · [HTTP API](docs/NATURAL_LANGUAGE.md) · [Semantic SQL](#native-semantic-sql) · [Full installation guide](docs/INSTALLATION.md)

### Use an existing PostgreSQL database

Keep your **PostgreSQL 17** database and query its tables in place. The application-only setup runs the workspace beside your server; it does not start a second PostgreSQL instance or require custom extension files. It adds jev4pg's catalog and runtime permissions. Business-table ownership stays unchanged, and attached sources are read-only through the workspace.

<details>
<summary><strong>Connect, install and attach an existing table</strong></summary>

From the release checkout above, run `python deploy/configure.py` if you have not already. Copy `deploy/external.env.example` to `deployment.env`. Set your existing host, port, database and login names. Follow the [connection setup](docs/EXTERNAL_POSTGRESQL.md#configure-the-connection) to supply the server CA certificate, migration-owner password and runtime password. Generated passwords do not change existing logins. Back up the target database before migration.

```bash
docker compose --env-file deployment.env -f compose.external.yaml build app
docker compose --env-file deployment.env -f compose.external.yaml run --rm migrate migrate --check
docker compose --env-file deployment.env -f compose.external.yaml run --rm migrate
docker compose --env-file deployment.env -f compose.external.yaml --profile queries up -d --wait
```

For an existing `business.messages` table, run these grants as its owner or an administrator in that database. `semantic_runtime` is the runtime login in the supplied environment example; replace it if you chose another name.

```sql
GRANT USAGE ON SCHEMA business TO semantic_runtime;
GRANT SELECT ON business.messages TO semantic_runtime;
```

Register the table without copying rows:

```bash
docker compose --env-file deployment.env -f compose.external.yaml exec app jev4pg attach messages --tenant demo --schema business --table messages
```

`demo` matches the generated workspace token and query worker. For another tenant, use that same tenant in the token map, `SDD_QUERY_TENANT` and attachment command. Source PostgreSQL grants and row-security policies determine which rows are visible.

Open the workspace, connect with your token, select `messages` and run `SELECT COUNT(*) AS total FROM messages;` in SQL mode. You can then ask questions about its actual columns or [attach more tables and views](docs/EXISTING_DATA.md).

For synchronous semantic functions inside PostgreSQL, use the optional [native extension build and configuration](native/README.md#build). After installing its files and configuring the provider, enable it with `CREATE EXTENSION jev_native;` and grant callers access as documented. The separate [asynchronous SQL interface](docs/POSTGRESQL_INTERFACE.md) uses `jevsd_pg` and a worker. There is no `CREATE EXTENSION jev4pg` shortcut in this release.

</details>

## BIRD Challenging: 100 questions, 11 databases

The project's historical evaluation tackles the hardest difficulty category in [BIRD's cleaned development benchmark](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106). Across 100 questions, JEV planned with **zero LLM generation calls** at 88.1% lower estimated token cost than the LLM baseline. Hybrid selected context with JEV and used **20.8% fewer LLM input tokens**.

| Method | SQL answer matches | Median time | Estimated cost / 100 attempts |
| --- | ---: | ---: | ---: |
| JEV 1.13.0 | 20/99 (20.2%) | 8.55 s | $0.389 |
| GPT-5.6 Terra | 39/99 (39.4%) | 8.68 s | $3.262 |
| JEV + GPT-5.6 Terra | 34/99 (34.3%) | 14.23 s | $2.839 |

These are efficiency tradeoffs: the LLM baseline matched more answers, and hybrid took longer. All held proposals were scored; one unavailable reference leaves 99 scorable questions. Medians cover 94 cases with up to three cases in flight. Costs use frozen accounting rates; one hybrid request has unreported usage.

Measured on the frozen Python planner on 23 September 2026. This is a local historical comparison, not an official leaderboard score or a measurement of v0.7.0 or the Rust preview. [Methodology and archived metrics](docs/benchmarks/BIRD_CHALLENGING_100.md).

## Why choose jev4pg

### Define once, reuse across queries

Give an interpretation a name, type and reviewed definition. A feature such as `needs_action` becomes a virtual column for filtering, grouping and reporting. Your team can inspect its definition, version a change and correct a judgment against its source.

After defining and activating `needs_action`, use it through the application SQL interface:

```sql
SELECT id
FROM documents
WHERE SEMANTIC_FEATURE(body, 'needs_action');
```

The same feature can serve a work queue, a report and a natural-language question. One reviewed definition keeps those workflows consistent. [Create a semantic feature](docs/SEMANTIC_FEATURES.md).

### Reuse judgments when policies change

Raw answer probabilities are stored separately from acceptance thresholds. Tighten a threshold and replay compatible evidence with zero new inference. Corrections retain their source dependencies; changed inputs require fresh evidence.

The native evidence registry extends compatible reuse across PostgreSQL connections, matching the projected context, typed questions and evaluator revision. This avoids paying for the same eligible judgment again. [Evidence reuse](docs/NATIVE_EVIDENCE.md).

### Run independent judgments in parallel

Questions sharing a context can share one request. Independent records and semantic branches can run concurrently within their budgets. In native stage plans, shared SQL stages materialize once and dependent work starts when its own inputs are ready.

Put exact filters and only required columns inside the source SQL to reduce data sent for inference. Batching and parallel scheduling then reduce repeated context and unnecessary waiting, while PostgreSQL computes the joins, aggregates and windows. [Parallel stage plans](docs/NATIVE_PLANS.md).

### Search with dimensions you can explain

Choose questions such as “Requests a refund?” and “Issue resolved?” Native JEV embeddings represent each record as an answer-probability matrix and vector with named dimensions. Reviewers can inspect which criteria make two records similar.

Project existing decisions into a matrix or compare compatible stored vectors locally, with no additional model call. You control the question basis that defines similarity. [Probability embeddings](docs/NATIVE_EMBEDDINGS.md).

### Give the LLM a focused job

Hybrid planning selects relevant schema and evidence with JEV before asking an LLM to propose SQL. JEV then reviews operations, populations, formulas and missing context in parallel. This focuses generation on a smaller context while keeping the proposal open to independent checks.

With concept generation and repair disabled, a request uses one LLM generation; JEV selection and review are accounted for separately. Users can inspect and correct the retained proposal. [Hybrid planning](docs/HYBRID_QUERY.md).

### Keep uncertain decisions reviewable

`VALUE`, `UNKNOWN` and `NOT_EVALUATED` stay separate from execution failures. A skipped judgment cannot silently become false or produce a misleading zero total. Applications can route uncertainty to review, retain held SQL for correction and preview proposed writes before an explicit commit. [Decision states](docs/JEV_OPERATORS.md).

Native stage plans, cross-connection evidence reuse and probability embeddings are optional preview features in v0.7.0. Reusable semantic features and hybrid planning run through the application. See [installation options](#choose-an-installation) for the released and native interfaces.

## What you can build

| Application                                     | What users can do                                                                       | Why jev4pg fits                                                                                                                                                                                  |
| ----------------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Support and operations queues                   | Find unresolved requests, connect them to account data and prioritize follow-up.        | The queue and its reports share one reviewed definition; uncertain cases remain visible.                                                                                                         |
| Document intake                                 | Extract typed fields from incoming text, inspect source passages and approve an import. | Field descriptions drive extraction, reducing manual entry while preserving a transactional review step. [Guide](docs/TEXT_IMPORT.md).                                                           |
| An analyst workspace in your product            | Ask in English or Simplified Chinese, inspect SQL and revise a saved interpretation.    | The HTTP API exposes context selection, generation and review as a reusable workflow. [Tutorial](examples/nl2sql/README.md).                                                                     |
| Search by business criteria                     | Find records similar in urgency, intent or resolution status.                           | Named probability dimensions make the comparison inspectable; stored compatible vectors can be compared without model calls. Native preview. [Example](examples/operators/native_embedding.sql). |
| Semantic analysis over existing PostgreSQL data | Add meaning-based queries to authorized tables and views in place.                      | Read-only attachments preserve source types and access controls without requiring a full data copy. [Setup](docs/EXISTING_DATA.md).                                         |

## Fit into your existing stack

| Feature                     | Available interface                                                                                                                                |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| 41 semantic operators       | Filtering, extraction, ranking, matching, verification and workflow composition through the [operator API](docs/JEV_FUNCTION_REFERENCE.md).        |
| PostgreSQL integration      | Asynchronous `jev.*` jobs in the release; direct Rust `jev_native.*` execution in the preview. [SQL interfaces](docs/POSTGRESQL_INTERFACE.md).     |
| Workspace and query history | Separate English and Simplified Chinese interfaces, editable interpretations and reviewed data changes. [User guide](docs/USER_GUIDE.md).          |
| Provider choice             | TypeSafe, compatible hosted HTTP endpoints and local Python adapters through the same typed decision contract. [Configuration](docs/PROVIDERS.md). |

Previously published as JevSDSQL and jevsd-pg; the current project and development package are named jev4pg.

## Choose an installation

| Path                     | Includes                                                                                | Setup                                                                                 |
| ------------------------ | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Application: `v0.7.0` | Workspace, HTTP API, background queries, source attachments and asynchronous `jev.*` SQL jobs          | [Compose or existing PostgreSQL](docs/INSTALLATION.md)                                |
| Optional native preview | Rust `jev_native.*`, parallel stage plans, evidence reuse and probability embeddings | [Native Compose stack](docs/NATIVE_DEPLOYMENT.md) or [source build](native/README.md) |

The native extension is a development preview for PostgreSQL 17 on Linux. It can run directly from SQL without Python. Use the native Compose overlay to build and enable it; the default Compose stack uses Python. Native maintained features and semantic write review remain on the [roadmap](docs/IMPLEMENTATION_PLAN.md).

Use a release tag for a fixed deployment and `main` to evaluate ongoing development. See the [changelog](CHANGELOG.md) for changes and the [upgrade guide](docs/INSTALLATION.md#upgrade) for component compatibility.

Follow the [quick start](#quick-start) for a new deployment, or [connect your existing PostgreSQL database](#use-an-existing-postgresql-database). The full [installation guide](docs/INSTALLATION.md) covers process-supervisor deployment, upgrades and backups.

## From question to SQL

> For each supplier, show the total quantity delivered, largest total first.

This request produced the following query in the [runnable tutorial](examples/nl2sql/README.md):

```sql
SELECT
  SUM("r0"."quantity") AS "result_1",
  "r0"."supplier" AS "result_2"
FROM "deliveries" AS "r0"
GROUP BY "r0"."supplier"
ORDER BY SUM("r0"."quantity") DESC NULLS LAST
```

On the tutorial data, the result is Birch: 36, Aster: 30, Cedar: 8. SQL performs the calculation. See [query examples](docs/NL2SQL_EXAMPLES.md) for filtering and averages, or use `POST /ask`:

```json
{
  "question": "For each supplier, show the total quantity delivered, largest total first.",
  "dataset_ids": ["deliveries"],
  "planner_mode": "hybrid",
  "execute": false
}
```

Use a dataset ID or name from your catalog. Set `planner_mode` to `jev` for planning without LLM generation. [API usage](docs/NATURAL_LANGUAGE.md) covers authentication, review and execution.

## Native semantic SQL

After [installing the Rust extension](native/README.md), evaluate messages directly in PostgreSQL:

```sql
SELECT source->>'id' AS id, decisions->'action' AS decision
FROM jev_native.scan(
    'SELECT id, body FROM messages',
    '{"action":{"type":"noul","instructions":"The message requests further action.",
                "subject_column":"body"}}',
    '{"max_rows":500,"max_requests":500,"max_judgments":500,"concurrency":4}'
);
```

Put exact filters and required columns inside the source SELECT. Questions sharing context can share a request; independent contexts run concurrently within the budget. Use `scan_many` for independent populations or `execute_plan` for a [typed stage DAG](docs/NATIVE_PLANS.md). Dependencies wait for their inputs while unrelated work continues.

Results preserve `VALUE`, `UNKNOWN` and `NOT_EVALUATED` separately from operational status. A skipped branch does not become false. Exact calculations that require missing semantic decisions are held for review.

## Embeddings with named dimensions

Define questions such as “Does this message request action?” and “Is the issue resolved?” Then `jev_native.embed` returns every answer probability as a matrix and flattened vector. Noul questions have false/true dimensions; Choice and Score retain all declared answers.

Use `jev_native.answer_matrix` to project existing decisions and `jev_native.embedding_distance` to compare complete, compatible embeddings. Both run locally without model calls. Stored vectors retain their question basis and evaluator identity, so incompatible revisions cannot silently mix.

See the [embedding guide](docs/NATIVE_EMBEDDINGS.md) and [runnable SQL example](examples/operators/native_embedding.sql). Retrieval quality depends on the basis and provider; current execution tests do not establish a retrieval-quality or speed advantage.

## Documentation and contributions

The [documentation index](docs/README.md) covers operators, native plans, evidence, installation and examples. [Performance and cost](docs/PERFORMANCE_AND_COST.md) explains execution accounting and what to measure. The [roadmap](docs/IMPLEMENTATION_PLAN.md) records current native limitations.

To report a problem, include a small synthetic dataset, the request and the expected result. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and checks.

## License

[Apache 2.0](LICENSE). Third-party software retains its own licenses. See [NOTICE](NOTICE) and [dependencies](docs/DEPENDENCIES.md).
