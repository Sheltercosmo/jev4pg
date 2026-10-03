# Native database implementation plan

The goal is a coherent semantic PostgreSQL database, with inspectable natural-language planning, native relational execution and reusable evidence. This plan is not complete when a Rust library compiles or a small example works.

## Work and acceptance gates

| Work | Required evidence |
| --- | --- |
| Correct relational execution | Reproduce unsafe rewrites, repair only proven scopes, and compare results on PostgreSQL with NULLs, duplicates, empty inputs, correlations and unrelated subqueries. |
| Native semantic scan | Execute through PostgreSQL without Python row materialization. Test bounded source batches, exact structured filtering, projection, caller permissions, cancellation, memory use and all three output states. |
| Evidence and source identity | Persist raw observations separately from policies. Test compatible reuse, provider/context/source changes, concurrent claims, row deletion and consistent population snapshots. |
| Shared execution DAG | Retain parallel independent judgments and real dependencies. Test conditional branches, cross-process budgets, retries, cancellation and lease fencing without duplicated provider work. |
| Relational semantic operations | Compose filtering, scoring, ranking, joining and extraction with SQL. Prevent incomplete populations from masquerading as exact counts, absence or mutation eligibility. Preserve supported public operators and declare their physical strategies. |
| Catalog and planner integration | Read authorized PostgreSQL schema and existing tables without requiring a full copy. Route generated plans and corrected history through the new execution module. Preserve English, Simplified Chinese and schema-renaming behavior. |
| Maintained features and writes | Test revisions, review, incremental refresh and atomic publication. Validate mutations against the exact reviewed source/evidence scope. |
| Installation and migration | Build versioned native artifacts and containers. Test clean install, 0.6.0 upgrade, backup restore, restart and standard PostgreSQL clients. Keep the web workspace optional for SQL users. |
| Comparative evaluation | Separate development, validation and a frozen final test. Compare old/new execution latency, peak memory, provider requests, tokens and exact results. Include selective and broad scans, repeated reads, updates, and complex SQL across unrelated schemas. |
| Public release | Publish concise operator, architecture, installation and measured performance documentation only after the gates pass. |

## Execution placement

PostgreSQL performs exact relational filtering and projection before semantic dispatch. Missing independent observations are batched by shared context, then scheduled concurrently under one admission policy. Dependent stages wait only for their inputs. Typed outcomes return to PostgreSQL for joins and arithmetic. Maintained evidence removes model latency from repeated transactional queries.

The native path and legacy path coexist during development. They are compared through the same external contracts; a fallback must be visible, never represented as native execution. The live 0.6.0 installation stays on the verified release while this checkout changes.

## Initial findings

The scalar-aggregate optimizer passed a whole AST to SQLGlot after validating only one subtree. A sibling aggregate EXISTS was changed incorrectly: an empty scalar aggregate still produces a row, whereas the rewritten grouped join did not. That reproducer is now a regression, not a final evaluation case.

The released semantic population snapshots and per-batch freshness checks materialize full rows. Provider throttling and duplicate suppression are process-local. Its PostgreSQL interface is an asynchronous queue. These are architectural gaps; adding a different implementation language alone does not close them.

The development native scan builds on PostgreSQL 17 and passes direct SQL integration checks. Ordinary `SEMANTIC` reads now use it through the existing query service, including generated SQL. Source selection and relational consumption share a PostgreSQL snapshot. Query allowances span datasets, and durable daily reservations are shared with Python requests. Saved observations support local policy replay.

The compiler groups questions by required source population and permits incomplete results only within a proven row-local fragment. It rejects unsupported alias lineage and direct evaluation on synthetic outer-join or subtotal rows. Explicit base-source evaluation remains composable with subsequent joins and grouping. These boundaries protect correctness while broader evaluation-site support is developed.

Independent source populations now feed one native scheduler with shared query admission and cross-source context reuse. The optional native registry adds automatic reuse across queries, separately committed observations, shared provider admission and request reconciliation. Source rollback does not erase a committed observation or authorize another uncertain dispatch. The [registry guide](NATIVE_EVIDENCE.md) documents configuration, receipts and limits.

Dependent-stage integration, reuse across differently packed questions, live source revisions, maintained features and aggregate resource accounting remain open gates. Versioned installation packages, upgrade and restore checks, comparative measurements and a frozen final evaluation are also required before release.
