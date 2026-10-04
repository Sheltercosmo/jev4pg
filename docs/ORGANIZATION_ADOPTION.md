# Organization deployment and migration

The target is gradual adoption: preserve existing SQL applications and source ownership, add semantic reads, validate results and operating limits, then enable reviewed writes where supported. Installing a semantic layer should not require moving every source table or replacing PostgreSQL tooling.

## Current support

The development application supports a dedicated PostgreSQL 17 database, restricted runtime roles, tenant policies, mounted secrets, transactional catalog upgrades and read-only source attachments within that database. The native preview adds direct SQL execution and a durable evidence registry. Installation preflight refuses conflicting objects before migration changes anything.

This is not yet a supported high-availability or cross-database migration product. The Compose stack is a reference deployment. Existing source registration does not provision network access, replicate remote rows or copy remote authorization policies.

## Implementation sequence

| Step | Deliverable | Acceptance |
| --- | --- | --- |
| Safe installation | Shared read-only preflight, explicit schema resolution and version contracts. | Unmanaged objects remain unchanged; released installations upgrade without data loss. Implemented in development. |
| Catalog isolation | Explicit application catalog schema and a transactional legacy migration, plus deployment against an externally managed PostgreSQL server. | Custom search paths cannot redirect runtime queries; source ownership and grants remain intact; rollback and restore succeed. |
| Source onboarding | A declarative, inspectable attachment manifest with selected columns, keys, relationships and tenant scope. | Preview performs metadata reads only; apply is atomic and repeatable; schema drift requires an explicit new binding. |
| Remote sources | Separate source adapters for federated reads and replicated local data, each declaring consistency and freshness. | Two-server tests cover disconnects, schema changes, source authorization, duplicate keys and stale evidence. |
| Operations | Documented connection and inference budgets, metrics, cancellation, rolling service changes, backup restoration and extension distribution. | Concurrent workloads stay within limits; uncertain calls are not silently repeated; recovery is verified on a second host. |
| Maintained semantics | Native typed generations with source-version checks and attributable review overlays. | Parallel evaluation publishes atomically; stale workers cannot overwrite newer generations. |

No step may narrow the planner to a dataset or sector. Keep installation, source access and provider transport separate from the stage DAG. Exact relational filtering precedes inference only where the source contract makes it safe.

## PostgreSQL mechanisms to build on

PostgreSQL's [foreign-data wrapper](https://www.postgresql.org/docs/17/postgres-fdw.html) provides remote tables, user mappings and query pushdown. A future adapter should use these facilities with an explicit remote snapshot and authorization contract. Supporting local attachments does not establish safe foreign-table support.

[Logical replication](https://www.postgresql.org/docs/17/logical-replication.html) is a candidate for maintaining a local copy during gradual adoption. Its replication state, schema handling and cutover checks must be part of the integration; do not promise automatic or zero-downtime migration without testing them.

PostgreSQL major-version upgrades belong to established PostgreSQL procedures such as [pg_upgrade](https://www.postgresql.org/docs/17/pgupgrade.html). Application catalog migrations and native extension updates remain separate operations. Record and validate all three versions before a production change.
