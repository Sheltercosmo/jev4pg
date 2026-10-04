# Reviewed source onboarding

## Decision

Use a declarative source manifest and a separate review artifact. PostgreSQL supplies types, keys and relationships; the manifest selects physical relations, exposed columns, logical names and descriptions. Do not infer source ownership, tenant predicates or uniqueness from samples.

Preview uses a read-only transaction and metadata inspection. Apply verifies the saved contracts, locks the tenant's catalog namespace and publishes the whole batch in one transaction. Proposed dataset IDs make repeated and concurrent applies idempotent without a separate job table. Rebinding retires the reviewed old attachment and creates a new ID; it never rewrites source tables or historical references. Imported datasets cannot be replaced by this path.

Catalog listing reads datasets and their bindings in one statement, preventing mixed visibility during attachment publication. Attach, detach, import and manifest apply share the catalog namespace lock. Independent JEV work remains in the query DAG; onboarding introduces no inference or model-dependent ordering.

## Database identity

Relation OIDs are insufficient after a restore. Bind source contracts to the cluster's system identifier, current database OID and current runtime-role OID. Read the identifier from PostgreSQL, not a value copied inside the application backup. Reject mismatched or missing identities before source rows or semantic evidence are consumed. Legacy attachments require an explicit reviewed rebind; migration must not manufacture continuity by backfilling the current identity.

The review artifact is an operator tool, not a new authorization credential. Its fingerprint detects edits; existing database permissions and trusted deployment access govern use. Physical replication retains lineage and requires separate failover qualification.

## Verification

Exercise metadata-only views that fail if rows are evaluated, complete composite keys, Chinese and renamed identifiers, NULLs and duplicates, stale reviews, late-write rollback and concurrent repeats. Restore a real dump into a separately initialized cluster, verify that attachments remain blocked, then rebind and query under the restricted runtime login. Test identity fields independently while preserving the original relation OID to cover coincident object IDs.

References: PostgreSQL [control data functions](https://www.postgresql.org/docs/17/functions-info.html#FUNCTIONS-CONTROLDATA), [SQL dump restoration](https://www.postgresql.org/docs/17/backup-dump.html).
