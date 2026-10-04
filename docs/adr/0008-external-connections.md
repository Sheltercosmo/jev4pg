# External PostgreSQL connections

## Decision

Keep server provisioning separate from application startup. Shared Compose service definitions support a bundled PostgreSQL stack and an external-server deployment. The external deployment starts only the application by default; migration and SQL workers are explicit choices. Only migration receives the administrator secret. External connections require certificate and hostname verification.

Connection ownership belongs to `Database`. Each instance owns a bounded main pool and a separate bounded source-guard pool. Source queries acquire a guard before their execution connection when view metadata must remain pinned. Ordinary table queries use only the main pool.

A guard pool per request would multiply connections without a process-wide limit. Sharing the main pool could deadlock if guards occupied every slot while waiting for execution connections. Separate shared pools preserve the required order and make capacity explicit. Tenant setup and transaction cleanup remain shared; checkout failure releases any acquired guard and its relation locks.

Both pools use the same credentials, TLS contract, checkout timeout and disconnect checks. Idle connection replacement does not replay interrupted transactions. Disposing the main engine also disposes the guard pool. It does not terminate transactions already checked out.

## Capacity and stages

Count both pools per `Database` instance, then account separately for process replicas, SQL clients, worker health checks and native registry connections. SQL worker lease renewal uses the main pool and needs spare capacity. These limits do not impose a serial JEV stage: independent semantic work retains its shared DAG after source validation.

## Verification

Real PostgreSQL tests hold all guard slots while using the main pool, reject overflow, verify tenant reset on reuse, release guards after main checkout failure and replace dead idle connections. TLS tests cover the main and guard pools, including wrong hostnames, missing or malformed roots and an untrusted CA. The deployment harness exercises authenticated HTTP access and readiness recovery after a server restart. Compose checks resolve the actual service definitions without claiming container-host qualification.

References: [PostgreSQL TLS](https://www.postgresql.org/docs/17/libpq-ssl.html), [SQLAlchemy pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html).
