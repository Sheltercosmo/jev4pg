# Native execution roadmap

The native preview moves semantic row execution into PostgreSQL while keeping natural-language planning and the workspace in Python. It is included as an optional preview in v0.7.0. The application release uses Python by default.

## Available in the preview

| Capability | Interface and behavior |
| --- | --- |
| Direct semantic scans | `scan` and `scan_many` use bounded PostgreSQL sources, concurrent requests and shared limits. |
| Typed stage plans | `execute_plan` combines SQL and semantic stages under one snapshot and scheduler. Independent stages remain parallel. |
| Conditional execution | Row guards and selected-branch merges preserve skipped work and uncertainty. The application compiler lowers supported SQL CASE expressions. |
| Query service integration | Opt-in native execution handles supported `SEMANTIC` reads over base columns, CTEs and derived relations. |
| Existing sources | Read-only attachments retain PostgreSQL types, privileges, row security and source contract checks. |
| Durable evidence | Optional registry storage separates observations from policy, coordinates requests and reuses compatible results. |
| Probability embeddings | A fixed question basis produces matrices and vectors; projection and compatible distance comparisons run locally. |
| Native container deployment | A Compose overlay builds the optimized extension, provisions the registry and enables native application reads. Extension 0.2.0 adds backup registration with a 0.1.0 upgrade path. |
| Application upgrade | Catalog version 4 adds durable application query jobs. Upgrades retain version 3 catalog objects and relocate validated version 1 and 2 metadata into `sdd_catalog` transactionally. |
| Application query jobs | Durable SQL submission, actor-scoped idempotency, separate workers, running cancellation and lease-fenced result publication. Workspace controls preserve edited drafts and recover saved jobs from history. |
| Bounded application results | PostgreSQL byte checks precede driver decoding, adaptive cursor batches limit transfer, and exact result prefixes report row or byte truncation. Background jobs budget rows and metadata separately. |
| Catalog isolation | Runtime metadata is schema-qualified; SQLite translates only that namespace. Fresh installations preserve public-schema objects and grants. |
| Installation preflight | `migrate --check` inspects ownership and catalog contracts without writes. Migration refuses unmanaged collisions and fixes its schema resolution before installation. |
| Feature publication checks | Python refresh rechecks source populations, definition and review revisions, prior publication and worker ownership before committing a run reference. |

Build and usage are in the [native guide](../native/README.md). Exact language and plan boundaries are documented in [native plans](NATIVE_PLANS.md), rather than implied by a general SQL compatibility claim.

## Remaining work

The immediate priority is practical application use: efficient large-table reads, a usable analyst workspace and a runnable application path. The v0.7.0 application includes cursor pagination, reviewed CSV import and an [activity monitor](../examples/activity_app/README.md). Validate these with real PostgreSQL populations and concurrent requests before expanding organization and remote-source features. See [application contracts](APPLICATIONS.md).

Ordinary PostgreSQL writes now use [bounded reviewed targets](APPLICATION_WRITES.md), typed assignment previews and row locks. This removes the whole-table Python snapshot for those writes. Bulk jobs, native semantic mutations and maintained-feature generations remain separate work; a successful bounded write does not establish their readiness.

Planning now uses [bounded shared value evidence](PLANNING_CONTEXT.md), concurrent table samples and index-checked literal probes. Sample completeness controls whether data can support a uniqueness hypothesis. These limits reduce preparation work; large-population semantic evaluation and durable bulk jobs remain unqualified.

The application provides [execution controls](QUERY_CONTROL.md) and [durable query jobs](QUERY_JOBS.md) for SQL reads and mutation previews. Jobs survive API disconnects, support cross-process cancellation and refuse publication by expired workers. Failed claims are not automatically retried. Queue retention, background natural-language planning, resumable bulk work and broader deployment qualification remain open.

| Area | Needed before a native production release |
| --- | --- |
| Relational coverage | Broader dependent query shapes and explicit provenance across joins and projections. |
| Evidence lifecycle | Reuse across differently packed questions, live source revision tracking and retention controls. |
| Maintained features and writes | Native typed generations, source-scope checks, human review overlays and semantic mutations. Python refresh has a publication gate; native refresh still needs its own source contract. |
| Resource accounting | Measure combined memory, connections and provider admission under concurrent workloads. |
| Distribution | Published binary packages and images, plus broader migration and recovery testing across hosts and PostgreSQL versions. |
| Organization adoption | Container-host qualification, a complete remote-source adapter and cross-host restoration. External connection checks and reviewed source manifests are implemented. The [remote acquisition protocol](REMOTE_POSTGRESQL_PROTOCOL.md) has two-server qualification but is not yet connected to application queries. See the [adoption plan](ORGANIZATION_ADOPTION.md). |
| Evaluation | Frozen comparisons of exact results, latency, memory and provider usage across unrelated schemas; separate language and embedding retrieval evaluations. |

## Design and evaluation rules

PostgreSQL filters and projects before inference where scope is proven safe. JEV batches independent questions; a consumer waits only for the results it needs. Observations remain separate from decisions, and missing work never becomes false. [Stage notes](JEV_PLANNING_STAGES.md) explain these placements.

Improvements must use catalog metadata, types and declared meaning. Production logic must not identify benchmark questions or expected answers. Develop and select changes on development and validation cases, then freeze the implementation before a final test. Previously inspected failures become regressions. Check paraphrases, Simplified Chinese, schema renaming, NULLs, duplicates and ties where relevant.

Deterministic tests establish execution contracts. They do not establish natural-language generalization, retrieval quality or a performance advantage over an LLM.
