# Catalog schema isolation

## Decision

Application metadata belongs to `sdd_catalog`. Declare that schema on shared SQLAlchemy metadata so selects, mutations, foreign keys and DDL resolve explicitly. Translate only `sdd_catalog` to the default namespace for the SQLite development adapter. Physical user tables use separate metadata and retain their registered schemas.

Catalog version 3 accepts validated version 1 and 2 installations in `public`. Under the existing migration lock, move their tables, marker and guard functions with `SET SCHEMA`, then create missing tables under the original catalog owner and apply the current grants. All changes share one transaction. Keep `sdd_data`, attached business tables and the `jev` SQL queue in place. Reject ambiguous markers and unmanaged target schemas instead of inferring ownership from table names.

Fresh installations do not alter public-schema privileges or adopt same-named business objects. The runtime receives catalog usage and table-level permissions, never catalog CREATE or owner membership. Direct attachments and view dependencies must not expose internal schemas.

This follows PostgreSQL's [schema relocation](https://www.postgresql.org/docs/17/sql-altertable.html) and SQLAlchemy's [explicit metadata schema](https://docs.sqlalchemy.org/en/20/core/metadata.html#specifying-a-default-schema-name-with-metadata) mechanisms. Catalog isolation belongs behind the database and migration interfaces; callers do not set a search path to find application state.

## Deployment contract

Stop application processes and coordinate administrative DDL before upgrading. Relation relocation takes table locks. Old processes cannot use the new layout. Do not add public compatibility views: they would retain name collisions and obscure version incompatibility. A failed transaction restores the previous layout; reversing a committed upgrade requires restoring its backup with the matching application.

This does not supply high availability, cross-database replication or safe migration under concurrent administrative writes. Dedicated databases remain the documented deployment recommendation until external-server operations and recovery are validated more broadly.

## Verification

Use real PostgreSQL connections to verify fresh coexistence, custom and Chinese search paths, temporary shadow tables, tenant separation, evidence guards, legacy object identities, grants, history and late-failure rollback. Exercise populated released-source upgrades independently of synthetic layout fixtures. SQLite persistence remains part of the regression suite. These checks establish deployment and execution contracts, not language accuracy or model performance.

## Stage placement

Migration precedes service startup. Schema qualification adds no model calls, serial semantic stages or changes to `VALUE`, `UNKNOWN` and `NOT_EVALUATED`. Independent work retains its shared stage DAG.
