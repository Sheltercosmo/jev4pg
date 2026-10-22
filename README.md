<p align="center">
  <img src="docs/assets/readme-banner.png?v=jev4pg" width="1000" alt="jev4pg: Natural language. Semantic SQL. Pangolin mascot with three parallel data paths." />
</p>

<h1 align="center">jev4pg</h1>

<p align="center">
  <strong>Natural language to SQL and semantic operators for PostgreSQL</strong>
</p>

<p align="center">
  <a href="https://github.com/Sheltercosmo/jev4pg/releases/tag/v0.8.0"><img src="https://img.shields.io/badge/release-0.8.0-18181b?style=flat-square&amp;labelColor=52525b" alt="Release 0.8.0" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-18181b?style=flat-square&amp;labelColor=52525b" alt="Apache 2.0 license" /></a>
</p>

<p align="center">
  <a href="https://jev4pg.com">Website</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#use-an-existing-postgresql-database">Existing PostgreSQL</a> ·
  <a href="#why-choose-jev4pg">Advantages</a> ·
  <a href="#performance-highlights">Performance</a> ·
  <a href="#bird-challenging-100-questions-11-databases">NL2SQL benchmark</a> ·
  <a href="#what-you-can-build">What you can build</a> ·
  <a href="https://jev4pg.com/guide/">Project guide</a> ·
  <a href="docs/INSTALLATION.md">Installation</a> ·
  <a href="docs/USER_GUIDE.md">User guide</a> ·
  <a href="docs/zh/USER_GUIDE.md">简体中文</a> ·
  <a href="docs/JEV_FUNCTION_REFERENCE.md">Function reference</a> ·
  <a href="docs/README.md">Documentation</a>
</p>

jev4pg brings semantic intelligence to PostgreSQL with **41 JEV operators** for filtering, extraction, ranking, matching and verification. Build semantic search, document workflows and analyst tools on your existing data, with natural-language queries in English and Simplified Chinese.

**Higher accuracy, lower latency.** In our paired semantic-filtering benchmark, jev4pg reached **98% accuracy versus 92%, with 36% lower median latency** than pg-jev's request path. Current development also delivers **20.4× faster cached operator lookups**, **3× faster request packing** and **3.7× faster native probability-matrix projection** against v0.8.0 in paired local microbenchmarks. Shared execution improvements support filtering, extraction, ranking, matching and verification by reducing evidence reads, repeated computation and request preparation. [Performance highlights](#performance-highlights).

**Explainable embeddings** represent data through named questions and answer probabilities, making similarity inspectable. The native preview combines these with **parallel execution and input deduplication**: independent work runs concurrently, while repeated values with the same context and questions share judgments, preserving every source row.

**Evidence caching** retains raw observations for compatible queries and threshold changes, with source and evaluator identity checks. **Controlled retries and reviewable fallbacks** keep errors, uncertainty and skipped work explicit, and preserve legal SQL proposals for correction. Reviewed definitions and corrections build a **self-developing semantic layer** your team can reuse across reports and applications. PostgreSQL handles joins, windows and exact arithmetic.

See it in action at [jev4pg.com](https://jev4pg.com), then build with the workspace, HTTP API or PostgreSQL interfaces.

## Performance highlights

### Faster operator execution

Current `main` optimizes the shared Python operator runtime and native 0.4.0 executor. Bulk evidence reads, exact-size request packing and direct probability projection reduce work across operator families.

| Operation | v0.8.0 | Optimized | Speedup |
| --- | ---: | ---: | ---: |
| Read 600 cached operator judgments | 162.1 ms | **8.0 ms** | **20.4×** |
| Pack 1,024 questions into requests | 22.5 ms | **7.4 ms** | **3.0×** |
| Project 10,000 probability matrices | 61.1 ms | **16.7 ms** | **3.7×** |

Cached operator reads drop from **600 SQL queries to 3**, while request packing produces the same 32 bundles. Native cache retention also removes **32 redundant provider requests** in the mixed recurring/new-context workload. Compatible evidence replay and stored-embedding comparisons need no new inference.

These are paired component measurements with a deterministic local provider, separate from the live accuracy comparison below. [All timings, cache tradeoffs and reproduction](docs/benchmarks/NATIVE_OPTIMIZATION.md) · [Optimizations by function](docs/NATIVE_OPTIMIZATION.md).

### Benchmark-leading semantic filtering

Native preview 0.3.0 packs independent judgments into parallel requests and reuses exact evidence as new records arrive. In a frozen, paired executor test, it was faster on seven of eight workloads against the pinned pg-jev 0.2.1 request path.

| Task, 400 records | jev4pg | pg-jev request path |
| --- | --- | --- |
| Real reviews | **98.0% accuracy**, 469 ms | 92.0%, 732 ms |
| Three independent predicates | **100% accuracy**, 1,498 ms | 99.33%, 1,983 ms |

The three-predicate case used **38 requests instead of 60**. With identical required-column projections on both sides, jev4pg used **36.2% fewer input tokens**. Warm passes made no provider calls on either side.

Measured with JEV 1.13, concurrency 16 and three repeats across 132 runs. Timings cover executor and HTTP paths. The full report includes every workload, token usage and held-answer scoring. [Full comparison and reproduction](docs/benchmarks/SEMANTIC_FILTERING.md) · [Use mapped SQL](docs/NATIVE_RECORD_MAP.md).

<p align="center">
  <img src="docs/assets/product-tour.gif?v=b87ec970" width="800" alt="Animated product tour: natural-language SQL, semantic filtering, text extraction, parallel JEV stages and probability embeddings." />
</p>

## Quick start

### Choose your setup

PostgreSQL stores your data and executes relational SQL in every setup. jev4pg adds two complementary components:

| Component | Use it for | Where it runs |
| --- | --- | --- |
| **Python application** | Natural-language queries, the web workspace, HTTP APIs, all 41 operators, reviewed features and query history | Beside PostgreSQL; no custom server extension required. |
| **Native Rust extension** | Synchronous semantic SQL, parallel stage plans, repeated-context reuse and explainable embeddings | Inside PostgreSQL 17 on Linux; works directly from SQL without Python. |
| **Combined deployment** | The workspace and natural-language planning with native semantic reads | Python plans and manages the workflow; Rust executes supported semantic stages in PostgreSQL. |

Use Python when you want a complete application or need to integrate with an existing server without installing native libraries. Use native execution when your application already speaks SQL and needs bounded semantic scans over PostgreSQL data. It keeps source reads and relational stages in the database and shares scheduling and evidence across semantic work. Both call your configured JEV provider.

`JEV` and `Hybrid` are **planning modes** in the application, independent of the execution engine. Hybrid adds LLM generation. The native extension consumes explicit SQL and typed questions; natural-language planning comes from the application.

The commands below use the **v0.8.0 release**, which includes native preview 0.3.0. Current `main` includes native preview 0.4.0. See [component compatibility](docs/INSTALLATION.md#upgrade) before upgrading.

### Python application

For a complete local workspace, install Python 3.11+ and Docker Compose v2:

```bash
git clone --branch v0.8.0 https://github.com/Sheltercosmo/jev4pg.git
cd jev4pg
python deploy/configure.py
docker compose build
docker compose up -d --wait
python deploy/configure.py --show-token
```

This starts PostgreSQL, the application and its workers. The setup prompt accepts a TypeSafe key; [provider settings](docs/PROVIDERS.md) cover other hosted or local models. Open [English](http://127.0.0.1:8000/ask/en) or [简体中文](http://127.0.0.1:8000/ask/zh) and connect with the workspace token printed by the last command. Hybrid mode also needs [LLM configuration](docs/HYBRID_QUERY.md#configuration). Ordinary SQL requires no model key.

Import a CSV or TSV, select the dataset and ask a question. For a ticket dataset:

> Which products have the most tickets describing unresolved billing problems?

Choose **Preview plan**, inspect the interpretation, then execute or revise it. SQL mode also accepts semantic predicates over registered datasets:

```sql
SELECT product, COUNT(*) AS unresolved_tickets
FROM tickets
WHERE SEMANTIC(body, 'The ticket describes an unresolved billing problem.')
GROUP BY product
ORDER BY unresolved_tickets DESC;
```

`SEMANTIC(...)` is application SQL syntax, accepted by the workspace and `POST /data/sql`. PostgreSQL clients use `jev_native.*` for synchronous execution. Inspect decision states and completeness before using an aggregate as a final result.

Browser imports support 5 MB and 10,000 rows. For larger data, load it with PostgreSQL tools and [attach the table](docs/EXISTING_DATA.md) without copying rows. [Workspace guide](docs/USER_GUIDE.md) · [Application API](docs/APPLICATIONS.md) · [Query examples](docs/NL2SQL_EXAMPLES.md).

### Use an existing PostgreSQL database

Run the Python application beside your PostgreSQL 17 server. With Python 3.11+, install the release package and create a connection profile:

```bash
python -m pip install https://github.com/Sheltercosmo/jev4pg/releases/download/v0.8.0/jev4pg-0.8.0-py3-none-any.whl
jev4pg setup
cd jev4pg-local
jev4pg --env-file admin.env migrate --check --sql-interface portable
jev4pg --env-file admin.env migrate --sql-interface portable
jev4pg serve
```

The wizard creates separate installation and runtime credentials. Migration adds the application catalog and portable SQL queue using ordinary PostgreSQL objects; it needs an owner with migration permissions. Open the workspace on port 8000 and use the token stored in `.secrets/api-tokens.json`. Grant the runtime login access to your source tables, then [attach them](docs/EXISTING_DATA.md).

For asynchronous operators from psql or DBeaver, [grant a SQL login and start `jev4pg sql-worker`](docs/EASY_SETUP.md#2-install-and-grant-access). For **Run in background** in the workspace, start a separate [query worker](docs/QUERY_JOBS.md#start-a-worker). The foreground workspace does not require either worker.

[Guided setup and first queued operator](docs/EASY_SETUP.md) · [Docker against an existing server](docs/EXTERNAL_POSTGRESQL.md).

### Native PostgreSQL extension

For a SQL-only application, [build and configure `jev_native`](native/README.md#build) on your PostgreSQL 17 Linux server, then enable it with `CREATE EXTENSION jev_native` and grant function and source access. Call it from psql, a SQL editor or any PostgreSQL driver. No Python service or application dataset registration is required. [First native SQL query](#native-semantic-sql).

For the **combined workspace and native deployment**, use the v0.8.0 checkout above and run these commands instead of the default Compose commands:

```bash
python deploy/configure.py --native
docker compose -f compose.yaml -f compose.native.yaml build
docker compose -f compose.yaml -f compose.native.yaml up -d --wait
python deploy/configure.py --show-token
```

This builds the extension, configures its provider and evidence registry, and starts the workspace with native semantic reads. The first build compiles Rust. This stack uses its own database volume and the same default ports as the default stack; choose one or configure separate ports. Keep both `-f` arguments in later Compose commands.

Native execution covers semantic reads, stage plans and probability embeddings. Use the Python engine for maintained semantic features and semantic write review. The 41-operator HTTP API and asynchronous `jev.*` jobs remain Python-backed in a combined deployment. [Native deployment guide](docs/NATIVE_DEPLOYMENT.md) · [Capability map](docs/CAPABILITIES.md#python-and-native-roles).

## BIRD Challenging: 100 questions, 11 databases

The project's historical evaluation tackles the hardest difficulty category in [BIRD's cleaned development benchmark](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106). Across 100 questions, JEV planned with **zero LLM generation calls** at 88.1% lower estimated token cost than the LLM baseline. Hybrid selected context with JEV and used **20.8% fewer LLM input tokens**.

| Method | SQL answer matches | Median time | Estimated cost / 100 attempts |
| --- | ---: | ---: | ---: |
| JEV 1.13.0 | 20/99 (20.2%) | 8.55 s | $0.389 |
| GPT-5.6 Terra | 39/99 (39.4%) | 8.68 s | $3.262 |
| JEV + GPT-5.6 Terra | 34/99 (34.3%) | 14.23 s | $2.839 |

These are efficiency tradeoffs: the LLM baseline matched more answers, and hybrid took longer. All held proposals were scored; one unavailable reference leaves 99 scorable questions. Medians cover 94 cases with up to three cases in flight. Costs use frozen accounting rates; one hybrid request has unreported usage.

Measured on the frozen Python planner on 23 September 2026. This is a local historical comparison, not an official leaderboard score or a measurement of v0.8.0 or the Rust preview. [Methodology and archived metrics](docs/benchmarks/BIRD_CHALLENGING_100.md).

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

For repeated values, [`scan_mapped`](docs/NATIVE_RECORD_MAP.md) forms a separate context dictionary for each question. Three and two distinct inputs need five judgments, even when they occur across thousands of rows. Missing judgments run together; cached probabilities map back to every original row. This opt-in layout is available in native preview 0.3.0.

### Search with dimensions you can explain

Choose questions such as “Requests a refund?” and “Issue resolved?” Native JEV embeddings represent each record as an answer-probability matrix and vector with named dimensions. Reviewers can inspect which criteria make two records similar.

Project existing decisions into a matrix or compare compatible stored vectors locally, with no additional model call. You control the question basis that defines similarity. [Probability embeddings](docs/NATIVE_EMBEDDINGS.md).

### Give the LLM a focused job

Hybrid planning selects relevant schema and evidence with JEV before asking an LLM to propose SQL. JEV then reviews operations, populations, formulas and missing context in parallel. This focuses generation on a smaller context while keeping the proposal open to independent checks.

With concept generation and repair disabled, a request uses one LLM generation; JEV selection and review are accounted for separately. Users can inspect and correct the retained proposal. [Hybrid planning](docs/HYBRID_QUERY.md).

### Keep uncertain decisions reviewable

`VALUE`, `UNKNOWN` and `NOT_EVALUATED` stay separate from execution failures. A skipped judgment cannot silently become false or produce a misleading zero total. Applications can route uncertainty to review, retain held SQL for correction and preview proposed writes before an explicit commit. [Decision states](docs/JEV_OPERATORS.md).

Native stage plans, cross-connection evidence reuse and probability embeddings are optional preview features in v0.8.0. Reusable semantic features and hybrid planning run through the application. See [setup choices](#choose-your-setup) for the released and native interfaces.

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
| PostgreSQL integration | Asynchronous Python-backed `jev.*` jobs or synchronous Rust `jev_native.*` functions. [Interface selection](docs/CAPABILITIES.md#python-and-native-roles). |
| Workspace and query history | Separate English and Simplified Chinese interfaces, editable interpretations and reviewed data changes. [User guide](docs/USER_GUIDE.md).          |
| Provider choice             | TypeSafe, compatible hosted HTTP endpoints and local Python adapters through the same typed decision contract. [Configuration](docs/PROVIDERS.md). |

Previously published as JevSDSQL and jevsd-pg; the current project and development package are named jev4pg.

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
