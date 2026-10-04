<p align="center">
  <img src="docs/assets/readme-banner.png?v=jev4pg" width="1000" alt="jev4pg: Natural language. Semantic SQL. Pangolin mascot with three parallel data paths." />
</p>

<h1 align="center">jev4pg</h1>

<p align="center">
  <strong>Natural language to SQL and semantic operators for PostgreSQL</strong>
</p>

<p align="center">
  <a href="https://github.com/Sheltercosmo/jev4pg/releases/tag/v0.6.0"><img src="https://img.shields.io/badge/release-0.6.0-18181b?style=flat-square&amp;labelColor=52525b" alt="Release 0.6.0" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-18181b?style=flat-square&amp;labelColor=52525b" alt="Apache 2.0 license" /></a>
</p>

<p align="center">
  <a href="https://jev4pg.com">Website</a> ·
  <a href="https://jev4pg.com/guide/">Project guide</a> ·
  <a href="docs/INSTALLATION.md">Installation</a> ·
  <a href="docs/USER_GUIDE.md">User guide</a> ·
  <a href="docs/zh/USER_GUIDE.md">简体中文</a> ·
  <a href="docs/JEV_FUNCTION_REFERENCE.md">Function reference</a> ·
  <a href="docs/README.md">Documentation</a>
</p>

jev4pg is an open-source semantic database built on PostgreSQL. It combines natural language to SQL (text-to-SQL) in English and Simplified Chinese with 41 JEV operators for filtering, extraction, ranking and verification, bringing structured records and free-form text into one query workflow. PostgreSQL handles joins, calculations and transactions; JEV evaluates meaning through typed decisions. Useful definitions and corrections become reviewed, reusable database features.

Use the web workspace, HTTP API or SQL clients. Choose JEV planning or combine LLM plan generation with JEV context selection and review, then inspect the SQL and refine saved queries before execution.

Visit [jev4pg.com](https://jev4pg.com) for interactive examples of natural-language queries, parallel execution and probability embeddings.

<p align="center">
  <img src="docs/assets/product-tour.gif?v=b87ec970" width="800" alt="Animated product tour: natural-language SQL, semantic filtering, text extraction, parallel JEV stages and probability embeddings." />
</p>

## What makes jev4pg different

Define meaning once. Query it, review it and reuse it.

jev4pg connects natural-language planning, typed semantic decisions and reusable database features. The advantages below come from that execution model. Native plans, the durable evidence registry and probability embeddings are available in the development preview.

### Change decision thresholds without another model call

Keep the original answer probabilities separately from the policy that accepts or rejects them. A review team can tighten its acceptance threshold and reconsider stored observations locally. Compatible requests reuse evidence across PostgreSQL connections, reducing repeated inference while retaining the source context and model revision. [Evidence reuse](docs/NATIVE_EVIDENCE.md).

### Turn business definitions into reusable columns

Define concepts such as `needs_follow_up`, `return_requested` or a named urgency rubric, review examples, then use them in queries as virtual columns. The same definition can support filtering, grouping and maintained results across an application. Corrections remain attributable to their source and revision, so teams can build on reviewed knowledge. [Semantic features](docs/SEMANTIC_FEATURES.md).

### Run SQL and semantic stages in one parallel plan

The native executor combines relational and semantic stages in a shared dependency graph. Independent JEV branches overlap, questions about the same context share a request, and shared CTEs materialize once. Each consumer waits only for the evidence it needs. This supports multi-step analysis while avoiding unnecessary sequential model calls; PostgreSQL still performs the joins and arithmetic. [Native plans](docs/NATIVE_PLANS.md).

### Build embeddings whose dimensions you can explain

Choose the questions that define similarity, such as whether a message requests action or reports a resolved issue. Each record becomes a matrix of answer probabilities and a vector with named dimensions. Inspect those dimensions, project existing decisions and compare compatible vectors locally without further inference. This gives applications direct control over what their semantic representation measures. [Probability embeddings](docs/NATIVE_EMBEDDINGS.md).

### Make uncertainty usable in application logic

An unresolved judgment, a skipped branch and a failed request have different meanings. jev4pg preserves those distinctions through execution. Applications can route uncertain records for review, hold an exact aggregate when required decisions are missing, and require a separate commit for proposed writes. Users can correct a saved interpretation and query again with that context. [Operator states](docs/JEV_OPERATORS.md) · [Query review](docs/NATURAL_LANGUAGE.md).

## What you can build

| Build | What jev4pg adds |
| --- | --- |
| A support operations console | Classify requests by meaning, join them to account data, and reuse reviewed definitions across queues and reports. |
| A document intake application | Extract typed records from text with source passages for review, then import approved entries transactionally. [Example](docs/TEXT_IMPORT.md). |
| An analyst copilot | Ask in English or Simplified Chinese, inspect SQL and assumptions, and correct a previous query instead of starting over. [Tutorial](examples/nl2sql/README.md). |
| An evidence review workflow | Verify claims against supplied records and keep supported, unresolved and unexecuted checks visible. [Operators](docs/JEV_FUNCTION_REFERENCE.md). |
| Search with explicit criteria | Represent documents through a shared set of named questions and compare their answer distributions. Native preview. [Example](examples/operators/native_embedding.sql). |
| Semantic tools for an existing PostgreSQL database | Attach authorized tables and views in place, preserving source types and access controls. Read-only attachments in the development preview. [Setup](docs/EXISTING_DATA.md). |

## Features

| Feature | Available interface |
| --- | --- |
| 41 semantic operators | Filtering, extraction, ranking, matching, verification and workflow composition through the [operator API](docs/JEV_FUNCTION_REFERENCE.md). |
| JEV and hybrid planning | Use JEV planning, or let JEV select relevant context before LLM generation and review the resulting operations in parallel. [Hybrid workflow](docs/HYBRID_QUERY.md). |
| PostgreSQL integration | Asynchronous `jev.*` jobs in the release; direct Rust `jev_native.*` execution in the preview. [SQL interfaces](docs/POSTGRESQL_INTERFACE.md). |
| Workspace and query history | Separate English and Simplified Chinese interfaces, editable interpretations and reviewed data changes. [User guide](docs/USER_GUIDE.md). |
| Provider choice | TypeSafe, compatible hosted HTTP endpoints and local Python adapters through the same typed decision contract. [Configuration](docs/PROVIDERS.md). |

## Where it fits

Related projects emphasize different workflows: [Vanna](https://github.com/vanna-ai/vanna) provides database chat and text-to-SQL interfaces, [LOTUS](https://github.com/lotus-data/lotus) provides semantic and agentic bulk data operators, and [pgai](https://github.com/timescale/pgai) provides PostgreSQL embedding pipelines and a semantic catalog.

Choose jev4pg when you need semantic decisions to become queryable, reviewable and reusable parts of a PostgreSQL application. Its focus is the combination of typed uncertainty, reviewed virtual columns, parallel semantic plans and question-defined probability embeddings.

Previously published as JevSDSQL and jevsd-pg; the current project and development package are named jev4pg.

## Choose an installation

| Path | Includes | Setup |
| --- | --- | --- |
| Stable release: `v0.6.0` | Workspace, HTTP API, Python semantic runtime and asynchronous `jev.*` SQL jobs | [Compose or existing PostgreSQL](docs/INSTALLATION.md) |
| Development: `main` | The application plus Rust `jev_native.*`, source attachments and native SQL compilation | [Native Compose stack](docs/NATIVE_DEPLOYMENT.md) or [source build](native/README.md) |

The native extension is a development preview for PostgreSQL 17 on Linux. It can run directly from SQL without Python. Use the native Compose overlay to build and enable it; the default Compose stack uses Python. Native maintained features and semantic write review remain on the [roadmap](docs/IMPLEMENTATION_PLAN.md).

Use a release tag for a fixed deployment and `main` to evaluate ongoing development. See the [changelog](CHANGELOG.md) for changes and the [upgrade guide](docs/INSTALLATION.md#upgrade) for component compatibility.

To start the released application with Python 3.11+ and Docker Compose v2:

```bash
git clone --branch v0.6.0 https://github.com/Sheltercosmo/jev4pg.git
cd jev4pg
python deploy/configure.py
docker compose build
docker compose up -d --wait
```

Configuration creates local credentials and asks for a TypeSafe key. For another endpoint or local model, follow [provider setup](docs/PROVIDERS.md). Hybrid planning also needs an LLM provider.

Open the [English workspace](http://127.0.0.1:8000/ask/en) or [Simplified Chinese workspace](http://127.0.0.1:8000/ask/zh). Run `python deploy/configure.py --show-token` to retrieve your workspace token.

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
