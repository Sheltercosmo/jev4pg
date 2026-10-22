# Capabilities and operator selection

jev4pg separates language interpretation from data execution. JEV supplies bounded semantic decisions, while SQL and application code handle arithmetic, authorization, storage and workflow state. Use the table below to find the relevant API before consulting its full signature.

## Python and native roles

The Python application and Rust extension serve different jobs. PostgreSQL remains the database in both deployments: it owns storage, indexes, relational SQL and transactions.

| Requirement | Python application | Native Rust extension |
| --- | --- | --- |
| Natural-language questions and hybrid planning | Selects context, proposes SQL and retains review decisions. | Executes supported semantic stages from the application's compiled plan. |
| Workspace, HTTP API, imports and query history | Provides the application and its lifecycle. | Optional execution engine behind the same workspace. |
| All 41 JEV operators | Available through `POST /jev/call` and queued `jev.*` jobs. | Exposes a focused SQL function set, not the full operator API. |
| PostgreSQL client access | `jev.*` submits jobs; a Python worker processes them after commit. | `jev_native.*` returns results within the SQL statement. |
| Parallel semantic scans and stage plans | Coordinates bounded application workflows and provider batches. | Streams source rows in bounded batches; shares an executor across independent stages and contexts. |
| Repeated values and compatible evidence | Deduplicates operator work and reuses tenant-scoped evidence. | `scan_mapped` builds per-question context dictionaries; the evidence registry supports reuse across connections. |
| Named-question embeddings and local distance | Can use the native engine through PostgreSQL. | Builds probability matrices and compares compatible stored embeddings without new inference. |
| Maintained features and semantic write review | Supported with the Python semantic engine. | Use the Python engine for these workflows. |
| Hosted or local JEV models | Compatible HTTP endpoints or in-process Python adapters. | Compatible HTTP endpoints; local models need an HTTP interface. |
| Server installation | Runs beside PostgreSQL; portable setup needs schema and role permissions. | Requires native extension files on a supported PostgreSQL 17 Linux server. |

Choose the Python application for analyst tools, document intake, reusable semantic definitions and product APIs. It keeps model adapters and application workflows outside the PostgreSQL process and is the easiest path when you cannot install server libraries.

Choose the native extension for SQL-driven services, semantic scans over existing tables and explainable similarity queries. PostgreSQL reads sources under the caller's privileges; Rust handles bounded semantic work, request packing and reuse. Put exact filters and projections in the source SQL so fewer values need inference. Direct calls do not need a Python service or application dataset registration.

Combine them when users need natural-language planning and your application needs native semantic reads. Set `SDD_SEMANTIC_ENGINE=native` after installing the extension and application integration. This changes supported semantic query execution, not the implementation of every operator. The native Compose deployment configures this combination.

Planning mode is a separate choice: `planner_mode=jev` uses JEV-based planning; `planner_mode=hybrid` adds LLM generation. Neither mode selects Rust execution. Both still rely on PostgreSQL for relational calculations. See the [quick start](../README.md#quick-start) for setup choices.

### SQL interface names

| Name | Meaning | Required process |
| --- | --- | --- |
| `SEMANTIC(...)` | Application query syntax, compiled by the workspace or `/data/sql`. | Python API; optional native execution engine. |
| `jev.submit`, `jev.result` | Asynchronous operator queue, installed as ordinary objects or the `jevsd_pg` SQL extension package. | Python SQL worker. |
| `jev_native.scan`, `jev_native.execute_plan` | Synchronous functions supplied by the Rust extension. | PostgreSQL with `jev_native` installed. |

The `jevsd_pg` queue package and the `jev_native` Rust extension are different components. Installing the former does not enable native execution. Queue calls become visible to workers after commit; native calls perform semantic work during the calling statement.

## Query and data workflows

| Objective | Interface | Result |
| --- | --- | --- |
| Ask a question about registered datasets | `POST /ask` | SQL proposal, planning decisions and optional query execution. |
| Inspect or correct an interpretation | `POST /ask/review` | Revised plan with the user's selected decisions. |
| Revisit an earlier request | `/query-history` | Saved requests and linked revisions. Opening history does not execute a query. |
| Execute SQL | `POST /data/sql` | A read result or a mutation preview, with a completeness manifest. |
| Create records from a document | `POST /data/extractions` | Typed entries with exact source evidence, followed by optional import. |
| Save a reusable semantic definition | `/features` | Reviewed Boolean, category, score or extraction features. |

The [workspace guide](USER_GUIDE.md) covers these flows without requiring SQL knowledge. The [text import guide](TEXT_IMPORT.md) explains the row and column descriptions used by extraction.

## Native PostgreSQL functions

The development Rust extension runs directly in PostgreSQL. It does not require the Python service for these functions, and is separate from the asynchronous `jev.*` worker interface.

| Objective | Function | Guide |
| --- | --- | --- |
| Evaluate typed questions over a SQL source | `jev_native.scan` | [Native execution](../native/README.md#query) |
| Evaluate distinct contexts and map results back to rows | `jev_native.scan_mapped` | [Semantic dictionaries](NATIVE_RECORD_MAP.md) |
| Share scheduling across independent sources | `jev_native.scan_many` | [Independent sources](../native/README.md#independent-sources) |
| Compose SQL, semantic and conditional stages | `jev_native.execute_plan` | [Native plans](NATIVE_PLANS.md) |
| Apply policy to a saved observation | `jev_native.decide` | [Evidence and policy](../native/README.md#save-and-reconsider-evidence) |
| Require resolved Boolean membership | `jev_native.require_bool` | [Exact membership](../native/README.md#query) |
| Build a question-by-answer probability matrix | `jev_native.embed` | [Embeddings](NATIVE_EMBEDDINGS.md) |
| Project saved decisions without inference | `jev_native.answer_matrix` | [Decision reuse](NATIVE_EMBEDDINGS.md#reuse-existing-decisions-and-stage-graphs) |
| Compare complete compatible embeddings locally | `jev_native.embedding_distance` | [Similarity queries](NATIVE_EMBEDDINGS.md#store-and-search) |
| Compare one embedding with a bounded candidate batch | `jev_native.embedding_distances` | [Batch distance, native 0.4.0 development](NATIVE_OPTIMIZATION.md#exact-local-comparison) |

The application can compile supported semantic reads, including derived relations and CASE, into native plans with `SDD_SEMANTIC_ENGINE=native`. Maintained features and semantic mutation review still require Python. See [installation](INSTALLATION.md#native-preview) and the [roadmap](IMPLEMENTATION_PLAN.md).

## Operator families

Call operators through `POST /jev/call`. A population may be supplied inline or identified by a registered dataset scope. The [function reference](JEV_FUNCTION_REFERENCE.md) documents exact arguments and examples.

| Task | Operators | When to use them |
| --- | --- | --- |
| Make a typed decision | `PROMPT`, `NOUL`, `CHOICE`, `SCORE` | Test a proposition, select a named alternative or apply an ordered rubric. |
| Organize a population | `CLASSIFY`, `TAG`, `FILTER`, `CLASSIFY_HIERARCHY` | Assign categories, evaluate independent labels or retain matching records. |
| Compare and prioritize | `COMPARE`, `RANK`, `RERANK`, `COMPOSITE_SCORE` | Compare pairs, score a population or combine explicit criteria. |
| Extract source material | `EXTRACT`, `EXTRACT_DATE`, `FIND`, `STRUCTURE`, `SUMMARY_EXTRACTIVE`, `EXTRACT_TABLE` | Select evidence, normalize supported dates, segment documents or build typed rows. |
| Connect records | `JOIN`, `ALIGN`, `RELATE`, `MATCH`, `EVIDENCE_JOIN` | Evaluate semantic relationships, identity candidates and bounded event patterns. |
| Check claims and requirements | `VERIFY`, `COVER` | Distinguish support, opposition, conflict and insufficient evidence. |
| Calculate over decisions | `AGGREGATE`, `CONTRAST` | Compute supported statistics and compare declared populations. |
| Control execution | `ROUTE`, `RESOLVE`, `TRACE`, `STATE_SCAN`, `WORKFLOW` | Select a handler, stop once an answer is established, compose states or run conditional stages. |
| Manage reusable meaning | `DISCOVER`, `REVIEW`, `PROMOTE`, `MATERIALIZE`, `REFRESH` | Propose definitions, record corrections and maintain approved generations. |
| Build or inspect semantic work | `EVALUATE`, `ENSURE_SEMANTICS`, `SELECT_SCHEMA`, `PLAN_SQL`, `EXPLAIN_PLAN` | Submit explicit work, reuse evidence, select schema or inspect a proposed query and its cost. |

## Choosing the right scope

`FILTER` evaluates its supplied population. `RERANK` only orders the shortlist you give it, so its highest-ranked item is not necessarily the best item in a larger corpus. `RESOLVE` can stop early once a declared condition is established; untouched subjects remain `NOT_EVALUATED`.

`EXTRACT` and `SUMMARY_EXTRACTIVE` return source passages. `EXTRACT_TABLE` maps exact spans into typed fields. They should be used when traceability to the source matters. Missing facts remain unresolved rather than being filled with generated text.

`JOIN`, `ALIGN` and `RELATE` return evidence about relationships. They do not merge stored identities. `ROUTE` selects from an allowlist without executing the chosen handler. `PLAN_SQL` returns a query proposal without executing it.

## What self-development means

`DISCOVER` creates provisional concepts from source examples. A reviewer checks independent examples and uses `PROMOTE` to approve a revision. `MATERIALIZE` publishes a resolved generation for that revision, and `REFRESH` rebuilds affected values when dependencies change. Human corrections are recorded separately from model observations.

This lifecycle supports growing a reusable semantic layer while keeping the source, definition, model evidence and human decision attributable. Approval is required for promotion; the system does not silently rewrite its own schema or treat discovered concepts as established facts.

## Limits that affect use

Direct operator populations currently support up to 5,000 registered rows and require primary keys. Operators have additional bounds for branching, matching and evidence bundles; see the [operator guide](JEV_OPERATORS.md#boundaries). Cost and time estimates are planning aids, not guarantees.

Semantic decisions can be uncertain or fail operationally. Check `output_state`, `operation_state` and coverage before using a value in another operation. Unknown membership can block a write or prevent a result from being described as complete.

The default JEV service is external. [Compatible HTTP providers and local Python adapters](PROVIDERS.md) can use the same operator API. The project supplies database integration, operators, execution controls and the user interface; it does not bundle model weights. Hybrid mode adds a separately configured LLM provider.
