# Native execution roadmap

The native preview moves semantic row execution into PostgreSQL while keeping natural-language planning and the workspace in Python. It is available in the source tree; v0.6.0 remains the packaged application release.

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
| Application upgrade | A populated v0.6.0 database upgrades to catalog schema 2 on PostgreSQL 17, preserving data, credentials, evidence, history and job states. |
| Installation preflight | `migrate --check` inspects ownership and catalog contracts without writes. Migration refuses unmanaged collisions and fixes its schema resolution before installation. |
| Feature publication checks | Python refresh rechecks source populations, definition and review revisions, prior publication and worker ownership before committing a run reference. |

Build and usage are in the [native guide](../native/README.md). Exact language and plan boundaries are documented in [native plans](NATIVE_PLANS.md), rather than implied by a general SQL compatibility claim.

## Remaining work

| Area | Needed before a native production release |
| --- | --- |
| Relational coverage | Broader dependent query shapes and explicit provenance across joins and projections. |
| Evidence lifecycle | Reuse across differently packed questions, live source revision tracking and retention controls. |
| Maintained features and writes | Native typed generations, source-scope checks, human review overlays and semantic mutations. Python refresh has a publication gate; native refresh still needs its own source contract. |
| Resource accounting | Measure combined memory, connections and provider admission under concurrent workloads. |
| Distribution | Published binary packages and images, plus broader migration and recovery testing across hosts and PostgreSQL versions. |
| Organization adoption | Explicit catalog-schema isolation, a tested external-database deployment path, staged source onboarding, and cross-host restoration. See the [adoption plan](ORGANIZATION_ADOPTION.md). |
| Evaluation | Frozen comparisons of exact results, latency, memory and provider usage across unrelated schemas; separate language and embedding retrieval evaluations. |

## Design and evaluation rules

PostgreSQL filters and projects before inference where scope is proven safe. JEV batches independent questions; a consumer waits only for the results it needs. Observations remain separate from decisions, and missing work never becomes false. [Stage notes](JEV_PLANNING_STAGES.md) explain these placements.

Improvements must use catalog metadata, types and declared meaning. Production logic must not identify benchmark questions or expected answers. Develop and select changes on development and validation cases, then freeze the implementation before a final test. Previously inspected failures become regressions. Check paraphrases, Simplified Chinese, schema renaming, NULLs, duplicates and ties where relevant.

Deterministic tests establish execution contracts. They do not establish natural-language generalization, retrieval quality or a performance advantage over an LLM.
