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
  <a href="#why-choose-jev4pg">Advantages</a> ·
  <a href="#what-you-can-build">What you can build</a> ·
  <a href="https://jev4pg.com/guide/">Project guide</a> ·
  <a href="docs/INSTALLATION.md">Installation</a> ·
  <a href="docs/USER_GUIDE.md">User guide</a> ·
  <a href="docs/zh/USER_GUIDE.md">简体中文</a> ·
  <a href="docs/JEV_FUNCTION_REFERENCE.md">Function reference</a> ·
  <a href="docs/README.md">Documentation</a>
</p>

jev4pg makes the meaning of your data queryable in PostgreSQL. Build applications that understand requests, extract records from documents and search by business criteria, then join those decisions with ordinary tables. Its 41 JEV operators bring semantic filtering, extraction, ranking and verification into SQL workflows, with natural-language queries in English and Simplified Chinese.

The core advantage is reusable semantic work. JEV evaluates constrained questions and retains typed answers and probabilities. Reviewed definitions become virtual columns, compatible observations serve later queries, and independent judgments run in parallel. PostgreSQL performs the joins and exact calculations. Each useful interpretation can become part of the application's data model.

Explore the interactive demos at [jev4pg.com](https://jev4pg.com), or use the web workspace, HTTP API and PostgreSQL interfaces to build your own application.

## Why choose jev4pg

Its distinctive combination is a semantic layer you can query, a record of evidence you can reuse, and an execution model that separates model judgment from exact computation.

| Advantage | What it gives your application |
| --- | --- |
| Fewer repeated model calls | Store answer probabilities separately from acceptance thresholds. Tighten a review policy and replay compatible evidence with zero new inference. The native registry also reuses observations across PostgreSQL connections when context, questions and evaluator revision match. [Evidence reuse](docs/NATIVE_EVIDENCE.md). |
| Business definitions that work like columns | Define and review `needs_follow_up` once, then use it in filters, groups and reports. Versioned definitions and source-bound corrections give applications a shared interpretation they can inspect and reuse. This is the foundation of the self-developing layer. [Semantic features](docs/SEMANTIC_FEATURES.md). |
| Parallel execution for complex semantic queries | Batch questions sharing context, overlap independent JEV branches and materialize shared SQL stages once. Dependent work starts when its own inputs are ready. Multi-step analysis avoids unnecessary serial model calls while PostgreSQL owns joins, windows and arithmetic. [Native plans](docs/NATIVE_PLANS.md). |
| Embeddings with a business meaning for every dimension | Choose questions such as “Requests a refund?” and “Issue resolved?” Each record becomes an answer-probability matrix and vector. Inspect why records differ, project existing judgments and compare compatible vectors locally without another model call. [Probability embeddings](docs/NATIVE_EMBEDDINGS.md). |
| Focused LLM generation with independent review | JEV selects relevant schema and evidence before the LLM proposes SQL, then reviews operations, populations, formulas and missing context in parallel. Generation can be limited to one LLM call with concepts and repair disabled; JEV usage is accounted for separately. [Hybrid planning](docs/HYBRID_QUERY.md). |
| Uncertainty your application can act on | `VALUE`, `UNKNOWN` and `NOT_EVALUATED` remain distinct from execution failures. Route unresolved decisions to review, preserve held SQL for correction and require a separate commit for proposed writes. Missing judgments cannot silently become false matches or zero totals. [Decision states](docs/JEV_OPERATORS.md). |

Native stage plans, the durable native evidence registry and probability embeddings are development-preview features on `main`. Reusable semantic features and hybrid planning run through the application. See [installation options](#choose-an-installation) for the released and native interfaces.

<p align="center">
  <img src="docs/assets/product-tour.gif?v=b87ec970" width="800" alt="Animated product tour: natural-language SQL, semantic filtering, text extraction, parallel JEV stages and probability embeddings." />
</p>

## What you can build

| Build | Put the advantages to work |
| --- | --- |
| Support and operations queues | Identify unresolved requests from message text, join them to account data and reuse the same reviewed definition in queues and reports. Route uncertain cases to a person. |
| Document intake and enrichment | Describe the fields you need, extract typed entries with source passages and approve a transactional import. Turn incoming text into queryable records with less manual entry. [Guide](docs/TEXT_IMPORT.md). |
| An analyst workspace inside your product | Let users ask in English or Simplified Chinese, inspect proposed SQL and refine a saved interpretation. Expose the workflow through the HTTP API. [Tutorial](examples/nl2sql/README.md). |
| Search organized around your own criteria | Rank records using named questions and answer distributions. Reuse stored compatible vectors for local comparison and make the dimensions visible to reviewers. Native preview. [Example](examples/operators/native_embedding.sql). |
| A review and enrichment layer for existing PostgreSQL data | Attach authorized tables and views in place, preserving source types and access controls. Add semantic queries without copying every row into a second database. Read-only attachments in the development preview. [Setup](docs/EXISTING_DATA.md). |

### A business definition becomes part of a query

Define `needs_action` as “The author explicitly requests an action; exclude quoted requests from someone else,” then preview and activate it. In the application SQL interface, that reviewed definition can select records:

```sql
SELECT id
FROM documents
WHERE SEMANTIC_FEATURE(body, 'needs_action');
```

The definition, source dependencies and reviewer corrections stay attached to the feature. Reuse it in another query or refer to its name in natural language. This is how a useful interpretation becomes a repeatable application capability. [Define your first feature](docs/SEMANTIC_FEATURES.md).

## Features and integration

| Feature | Available interface |
| --- | --- |
| 41 semantic operators | Filtering, extraction, ranking, matching, verification and workflow composition through the [operator API](docs/JEV_FUNCTION_REFERENCE.md). |
| JEV and hybrid planning | Use JEV planning, or let JEV select relevant context before LLM generation and review the resulting operations in parallel. [Hybrid workflow](docs/HYBRID_QUERY.md). |
| PostgreSQL integration | Asynchronous `jev.*` jobs in the release; direct Rust `jev_native.*` execution in the preview. [SQL interfaces](docs/POSTGRESQL_INTERFACE.md). |
| Workspace and query history | Separate English and Simplified Chinese interfaces, editable interpretations and reviewed data changes. [User guide](docs/USER_GUIDE.md). |
| Provider choice | TypeSafe, compatible hosted HTTP endpoints and local Python adapters through the same typed decision contract. [Configuration](docs/PROVIDERS.md). |

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
