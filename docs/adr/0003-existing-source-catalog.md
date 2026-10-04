# Existing PostgreSQL sources

## Decision

Separate catalog registration from data ownership. Imports remain application-owned tables. An attachment is a read-only logical dataset backed by an existing relation, with a recorded physical identity and selected schema contract. Trusted operator commands and the SDK register sources; HTTP reviewers cannot register arbitrary physical relations.

## Execution boundary

Authorization resolves logical names before source validation. Direct SQL, generated SQL, history reruns and native plans share binding checks. PostgreSQL enforces runtime-role privileges and source row security. Selected columns constrain SQL qualification and provider context.

Table locks precede the repeatable-read snapshot. Views and materialized views use a separate guard connection: PostgreSQL rejects direct LOCK on materialized views, while preparing a SELECT already establishes a data snapshot. The guard's metadata-only SELECT holds relation locks; the main snapshot starts afterwards. Both connections close through one context manager. Ordinary tables need no extra connection.

Validate physical identity, selected columns, keys, comments and view-definition fingerprints while sources remain pinned. Every view dependency must use invoker security. Foreign sources need a separate snapshot contract. Inference remains downstream of validation and retains the shared stage DAG.

## Catalog lifecycle

Migration version 2 adds tenant-protected source bindings separately from imported records. Registration reads catalogs and checks access without enumerating rows. It does not modify source grants, policies, constraints or ownership. Composite foreign keys remain complete; hybrid retrieval retains their operands and bridge tables. Single-column planners receive only relationships they can represent.

Changed contracts require a new attachment identity. Detaching disables catalog access while preserving source data and evidence. Imported dataset metadata keeps its existing shape so established approvals remain valid.

## Costs and acceptance

Indirect sources occupy an additional connection during execution. Contract checks add metadata reads. Include both costs in capacity and latency measurements.

Acceptance covers source updates, selected columns, permissions, row security, UUIDs and decimals, composite keys, duplicates, NULLs, renamed and Chinese identifiers, schema replacement, nested views, refresh locks, cleanup after errors and migration. Deterministic execution tests do not establish natural-language accuracy.

References: [PostgreSQL LOCK](https://www.postgresql.org/docs/17/sql-lock.html), [view security](https://www.postgresql.org/docs/17/sql-createview.html).
