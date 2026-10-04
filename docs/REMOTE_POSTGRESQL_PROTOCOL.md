# Remote PostgreSQL acquisition protocol

Development work for the future federated-source adapter. The SQL helper and two-server qualification tests are implemented. Catalog registration, expression admission, native execution and evidence identity are not integrated yet. Application attachments still reject foreign tables. Use this protocol in disposable development databases while those checks are completed.

The helper uses PostgreSQL 17, `postgres_fdw`, ordinary relation locks and a shared advisory schema gate. It does not copy source rows, store credentials or call a model. Remote access uses the mapped PostgreSQL principal; local `sdd.tenant` settings do not propagate to that principal.

## Why a guard is needed

`postgres_fdw` normally opens a repeatable-read remote transaction. Its snapshot can precede its source locks. A concurrent `TRUNCATE` can therefore leave an old snapshot observing an empty relation. The qualification suite reproduces this behavior. PostgreSQL documents both the [FDW transaction model](https://www.postgresql.org/docs/17/postgres-fdw.html#POSTGRES-FDW-TRANSACTION-MANAGEMENT) and the [TRUNCATE exception to MVCC](https://www.postgresql.org/docs/17/mvcc-caveats.html).

A separate guard pins the source before the query's remote transaction starts. The query must acquire its own locks and then prove the original guard is still alive on the same remote server. A successful earlier probe alone is insufficient: the guard could disconnect between the probe and the query's locks.

```mermaid
sequenceDiagram
    participant C as Query coordinator
    participant G as Guard connection
    participant Q as Query connection
    participant R as Remote PostgreSQL
    C->>G: Acquire source
    G->>R: Shared schema gate, relation locks, token
    R-->>C: Source contract and guard token
    C->>Q: Start query transaction
    Q->>R: Acquire own gate and source locks
    Q->>R: Verify original token is still held
    R-->>C: Current contract and live proof
    C->>C: Validate source, mapping and expression scope
    Q->>R: Read approved source
    C->>Q: End query transaction
    C->>G: End guard transaction
```

The guard's own snapshot is disposable. Keep both transactions open until validation and reads finish. A failed proof aborts the query; it does not authorize a reconnect or a partial result. A query spanning multiple servers has a snapshot per server, not one globally atomic snapshot.

## Install and inspect

The remote deployment owner installs the helper once. Event-trigger installation requires a PostgreSQL superuser. The script is transactional and refuses an existing `jev_remote` schema instead of replacing it.

```sh
psql "$REMOTE_ADMIN_DSN" -v ON_ERROR_STOP=1 -f deploy/federation/remote_guard.sql
```

Create a metadata view for an authorized source. This view returns metadata, not business rows:

```sql
CREATE SCHEMA source_exports;
CREATE VIEW source_exports.orders_contract
WITH (security_invoker=true) AS
SELECT * FROM jev_remote.acquire('business.orders'::regclass);

GRANT USAGE ON SCHEMA business, source_exports, jev_remote TO source_reader;
GRANT SELECT ON business.orders, source_exports.orders_contract,
    jev_remote.active_guards TO source_reader;
GRANT EXECUTE ON FUNCTION jev_remote.acquire(regclass) TO source_reader;
```

The reader also needs SELECT on static view and row-policy dependencies. A materialized view exposes its stored rows and does not grant access to the tables from which it was refreshed. Source owners retain all table ownership and policy control.

On the query server, configure one `postgres_fdw` server and restricted user mapping. Import or declare the data relation, metadata view and `jev_remote.active_guards` through that same server and mapping. Configure verified TLS and connection, lock and statement timeouts using PostgreSQL's normal connection options. Credentials belong in the mapping or deployment secret configuration, never in a reviewed source contract.

The metadata foreign table has two `jsonb` columns, `contract` and `guard`. The proof foreign table has `pid integer`, `key_hi bigint`, `key_lo bigint`, `database bigint` and `role bigint`. The data table must retain the remote names, types, modifiers and collation semantics.

`jev_remote.acquire(regclass)` returns one row, including for an empty source:

| Field | Meaning |
| --- | --- |
| `contract.protocol` | Acquisition protocol version, currently 1. |
| `contract.origin` | Live cluster system identifier, database OID and mapped role OID. |
| `contract.oid`, `name`, `kind` | Remote source identity and physical name. |
| `contract.columns`, `constraints` | Types, comments, nullability and complete key operands. |
| `contract.relations` | Catalog-visible relation dependencies, view definitions and row policies. |
| `contract.dependency_state` | `VALUE` for the inspected table footprint; `UNKNOWN` when views or policies still require expression review. |
| `guard` | Transaction-bound backend and advisory-lock identity. This is temporary proof, not persistent evidence identity. |

After the query connection acquires its own locks, match every token field against `active_guards`. Zero matches means failure. Do not treat a returned contract, a matching cluster identifier or `dependency_state=VALUE` alone as authorization to execute arbitrary SQL. Data mapping, source approval, expression scope and query permissions are separate checks.

User-defined view and policy functions/operators currently stop acquisition. Builtins can also hide data access: for example, `query_to_xml` accepts SQL text. Every view or policy consequently remains `UNKNOWN` until an expression inspector can establish its data dependencies. The helper never turns an uninspected expression into a complete dependency claim. PostgreSQL's [dependency catalog](https://www.postgresql.org/docs/17/catalog-pg-depend.html) does not record every builtin reference.

## Schema maintenance and capacity

The helper's readers share advisory lock `(1246058067, 1)` within the remote database. A `ddl_command_start` event trigger acquires the exclusive form before schema changes. This prevents relation-name redirection and changing partition or inheritance membership during the protected read. Ordinary row updates can continue. Independent readers do not serialize on the gate.

The gate also delays unrelated schema DDL in that database, including temporary-table creation. This is a deliberate operational cost of the current protocol. Bound query lifetimes and schedule schema maintenance accordingly. Both reader acquisitions use a try-lock: queued DDL produces SQLSTATE `55P03`, allowing the coordinator to release the guard instead of waiting indefinitely behind its own dependency.

Event triggers do not cover changes to roles, databases, tablespaces or event triggers themselves. Gate removal, disabling, helper upgrades and shared-role maintenance require draining readers and taking the exclusive advisory gate first. Admission rejects an already disabled gate; it cannot protect against an administrator bypassing the protocol during a running query. See [event-trigger behavior](https://www.postgresql.org/docs/17/event-trigger-definition.html).

The helper rejects standby execution because WAL replay bypasses the schema gate. Actual standby failover is not qualified. Same-database loopback federation must use the existing local attachment path: local temporary DDL could otherwise wait on the request's remote gate. Separate remote snapshots, provider calls and shared evidence still need end-to-end qualification before federation is enabled in the application.

## Development checks

Configure two separately initialized disposable PostgreSQL 17 servers. The local server needs `postgres_fdw`; the remote server must accept password authentication for the generated `jev_fdw_reader_*` and `jev_fdw_other_*` logins. The fixtures create and remove their own databases and roles.

```sh
export SDD_TEST_ADMIN_URL=postgresql+psycopg://admin:password@localhost:5432/postgres
export SDD_TEST_PEER_ADMIN_URL=postgresql+psycopg://admin:password@localhost:5433/postgres
python -m pytest tests/test_remote_guard_postgres.py -q
```

The suite covers the unguarded race, guard handoff and termination, queued DDL, source grants, mapped role isolation, repeatable data reads, schema and inheritance changes, empty populations, duplicates, decimals and Chinese identifiers. It also checks that unknown expression dependencies stay unknown without evaluating their rows. These are execution-contract tests, not natural-language accuracy or throughput measurements.
