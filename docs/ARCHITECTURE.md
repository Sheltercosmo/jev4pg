# Architecture

jevsd-pg has three main responsibilities: interpret the request, establish the evidence needed by that interpretation, and execute authorized data operations. Keeping these responsibilities separate makes plans inspectable and allows independent semantic work to run in parallel.

## Components

| Component | Location | Responsibility |
| --- | --- | --- |
| HTTP service | `sdd/api.py`, `sdd/generic/api.py` | Authentication, tenant context and public query routes. |
| Catalog | `sdd/generic/catalog.py` | Dataset schemas, field descriptions, keys and relationships. |
| Existing sources | `sdd/generic/source_catalog.py` | Read-only relation attachments, physical contracts and snapshot guards. |
| Query planning | `sdd/generic/planner.py` and adjacent planner modules | JEV planning, typed stages, hybrid proposals and review. |
| SQL execution | `sdd/generic/sql.py` | SQL validation, semantic predicates, result coverage and mutation previews. |
| Native query compilation | `sdd/generic/native_sql.py`, `native_relational.py`, `native_conditionals.py` | Authorized source relations, shared stages and conditional SQL routing. |
| Native semantic execution | `native/core/`, `native/pg/` | Bounded Rust evaluation, concurrent provider I/O, typed decisions and observation replay. |
| Semantic features | `sdd/generic/features.py` | Definition revisions, evidence reuse, human corrections and materialization. |
| Operator API | `sdd/operators/service.py` | Operator contracts, authorization and dispatch. |
| Operator runtime | `sdd/operators/runtime.py`, `budget.py`, `types.py` | Batching, concurrency, reservations and explicit output states. |
| Workspace | `sdd/web/` | Separate English and Simplified Chinese interfaces. |

The Rust execution path is an opt-in development feature; Python remains the default semantic engine. PostgreSQL executes relational SQL in both paths. See the [native usage guide](../native/README.md) and [execution stage dependencies](JEV_PLANNING_STAGES.md).

## Implementation languages

PostgreSQL owns storage, transactions, indexes and relational execution through its existing native engine. The Rust extension runs semantic scans, shared request scheduling, conditional decisions and evidence reuse inside PostgreSQL. It can be called directly from SQL without the Python service.

Python handles the HTTP API, catalog integration, natural-language planning and compilation of application queries into native plans. Maintained semantic features, semantic mutation reviews and operators without a native strategy still use the Python runtime. The web workspace uses JavaScript, HTML and CSS. Shell scripts handle installation and service startup.

The running 0.6.0 release uses the Python semantic runtime and queued SQL interface. The development Rust extension is a separate deployment; its presence in the source tree does not mean the running service uses it.

## Hybrid stage placement

| Stage | What must already be known | Work that can overlap | Reason for its position |
| --- | --- | --- | --- |
| Definition retrieval | The objective and authorized catalog | Independent rule pages | Relevant definitions guide later field selection. |
| Field retrieval | Applicable definitions and their dependencies | Independent schema pages | Context is reduced before the LLM sees it; keys and relationship bridges survive. |
| Value observation | Retained tables and fields | Bounded source reads | Actual values help interpretation without becoming an execution filter. |
| SQL generation | The retained context and objective | One bounded generation | The LLM proposes executable alternatives and states its assumptions. |
| Validation and review | Candidate SQL and implementation facts | Coverage, output, population, relationship, formula and operation checks | Each check receives the same candidate context and does not wait for unrelated judgments. |
| Local alternatives | A supported, identified defect | Checks for distinct alternatives | Deterministic changes can avoid another generation while retaining the original candidate. |
| Optional repair | A concrete compilation or semantic defect | Independent replacement checks | At most one additional generation is permitted; uncertainty alone does not trigger it. |
| Execution | A legal plan and any required user confirmation | Independent semantic row decisions within limits | SQL performs arithmetic and relational operations after the necessary decisions are available. |

Shared stages form a DAG. A dependency is a reason to wait; the visual arrangement of a plan is not. Compatible questions share context in a request, while independent contexts use bounded concurrency.

## Evidence and state

Evidence identity includes tenant, source revision, model, context and question. Compatible observations can be reused when a policy threshold changes. A changed source or meaning requires new evidence. Human assertions remain attributable and do not overwrite the model's original observation.

`VALUE`, `UNKNOWN` and `NOT_EVALUATED` describe result availability. Operational states describe what happened while obtaining that result. A conditional branch that was not executed cannot supply a false value to a dependent stage.

## Data boundaries

Clients authenticate with a server-controlled token mapped to a tenant, actor and role. SQL binds registered catalog objects and applies the same access rules to direct and generated queries. PostgreSQL row-level security provides a second tenant boundary. The service itself is trusted to set the authenticated tenant context.

Ordinary reads use a database transaction. Semantic reads retain the source evidence needed to interpret their results. Mutation previews check authorization, source freshness and confirmation before committing. Unresolved semantic membership prevents writes.

## Extending the system

Add a reusable operator or planning mechanism through its typed inputs and result contract. Record when a new stage depends on another result and what work can remain independent. Keep dataset names and expected benchmark answers out of production decisions. See [contribution guidance](../CONTRIBUTING.md) for tests and documentation.

## Provider boundary

Provider transport sits below the typed stage DAG. Each dispatch receives one complete shared-context question batch. Independent batches retain the existing worker and tenant concurrency limits; changing an endpoint does not introduce a new planning barrier.

The HTTP and Python adapters return the same Noul, Choice and Score envelope. Validation occurs before evidence is stored. Failed or malformed responses remain operational failures, and skipped branches remain NOT_EVALUATED.

Cache identity includes endpoint or adapter, configured revision and model. Reviews, operator approvals and automatic refresh bind to that identity, so changing a provider cannot silently reuse its model judgments. Local predictors must support concurrent calls or serialize access internally when their runtime requires it.

## PostgreSQL jobs and deployment

The optional `jevsd_pg` SQL extension owns a private queue and login-to-tenant mappings. Its public security-definer functions use a fixed search path and the authenticated `session_user`. SQL clients receive function execution rights, not runtime table grants. An administrator installs the schema; the API and worker use a separate restricted role.

Submission must commit before a worker claims the job. After claiming, the worker releases its queue transaction and calls the existing OperatorService. Independent jobs run concurrently, and each operator retains its shared stage DAG, batching and budget controls. Queueing adds no dependency between unrelated semantic stages. Only result publication waits for the operator result and a valid lease.

Heartbeats run separately from inference. Expired work becomes FAILED / NOT_EVALUATED without automatic replay; a stale worker cannot publish success. Job state describes queue progress, while operator output and execution states retain their existing meanings. Durable evidence can survive an interrupted job and must be inspected before retrying side effects.

Packaged migrations create roles, grants, row-level security and immutable evidence guards transactionally. Compose starts API and worker only after migration succeeds. The [SQL interface guide](POSTGRESQL_INTERFACE.md) describes client access, states and recovery.
