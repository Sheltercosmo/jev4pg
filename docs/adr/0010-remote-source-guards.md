# Remote source acquisition before federation

Status: acquisition protocol implemented for qualification; application integration remains open.

Use `postgres_fdw` for remote PostgreSQL transport and pushdown. Do not relax the existing foreign-table attachment rejection until the remote adapter validates source identity, mapping, expression scope and evidence provenance together.

The coordinator needs two bounded local connections. The guard acquires the remote schema gate and source locks first. Only then may the query connection establish its remote snapshot, acquire its own locks and verify that the original guard token remains live. A proof before query-side locking leaves a guard-loss race. Checking origin alone cannot distinguish independently routed connections to physical replicas.

Ordinary relation locks do not freeze schema names or inherited populations. A database-scoped `ddl_command_start` gate gives readers compatible shared locks while supported schema changes require an exclusive lock. Try-lock reader admission prevents a queued DDL operation from creating a guard-to-query wait cycle. This costs additional remote connections and delays unrelated schema changes; it does not serialize readers or ordinary row updates.

The protocol uses invoker permissions and the mapped remote login. It never guesses how local tenants correspond to remote policies. Unknown view and policy expression footprints remain `UNKNOWN`, even when catalog dependencies and locks were acquired. Dynamic SQL builtins show why a dependency catalog is insufficient. The next adapter step must validate expressions and source mappings before SQL rows or model calls are allowed.

Trigger and role maintenance require coordinated draining. Standby replay and same-database loopback are excluded from this protocol. Multi-server reads retain a vector of remote snapshots; no distributed atomicity is implied. Guard tokens are transient liveness checks, not evidence cache keys.

The [protocol guide](../REMOTE_POSTGRESQL_PROTOCOL.md) defines installation, the return contract, operational limits and executable two-server qualification. Existing application and native source behavior remains unchanged until the full adapter is ready.
