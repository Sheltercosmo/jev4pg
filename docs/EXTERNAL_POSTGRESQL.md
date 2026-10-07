# Deploy with an existing PostgreSQL server

The application can run separately from PostgreSQL 17. This path starts no database container and creates no database volume. Your PostgreSQL administrator retains responsibility for backups, server upgrades, availability and source permissions. Use a dedicated database for the initial deployment.

The HTTP workspace requires the Python application and application catalog. The asynchronous SQL interface additionally needs the `jevsd_pg` extension files on the server. Managed services that cannot install custom extension files can use the HTTP path. Native Rust execution has its own [installation procedure](NATIVE_DEPLOYMENT.md).

Use the [README quick start](../README.md#quick-start) for a new bundled database. To connect an existing server, start from the released source:

```bash
git clone --branch v0.7.0 https://github.com/Sheltercosmo/jev4pg.git
cd jev4pg
```

Requires Python 3.11+ and Docker Compose v2. Run the remaining commands from that checkout. Back up the target database before migration.

## Configure the connection

Copy [the environment example](../deploy/external.env.example) to `deployment.env`. Set the existing server's hostname, port, database, restricted runtime login and migration owner. The database must already exist. Use a hostname reachable from the containers and covered by the server's TLS certificate; `127.0.0.1` inside a container refers to that container, not the host's PostgreSQL service.

```bash
python deploy/configure.py --no-prompt
```

Place the server's trusted CA certificate in `.secrets/database-ca.crt`. Set `.secrets/postgres_password` to the migration owner's existing password. If the runtime login already exists, set `.secrets/app_password` to its existing password too. The configuration script creates missing files; generated passwords do not change existing PostgreSQL logins. Files that already exist are preserved.

Set the provider key files and API principal map for your organization as described in [installation](INSTALLATION.md#credentials-and-providers). Keep administrator credentials separate from runtime credentials. The runtime login must not own the catalog or belong to administrative roles. A preprovisioned runtime login avoids requiring role creation during migration; the owner still needs database CREATE and ownership rights for the catalog.

The external Compose file mounts the CA certificate and uses `sslmode=verify-full`. This checks both the certificate chain and hostname, following [PostgreSQL's TLS contract](https://www.postgresql.org/docs/17/libpq-ssl.html). Certificate failures stop the connection. Do not replace verification with an unencrypted fallback.

## Install and start

```bash
docker compose --env-file deployment.env -f compose.external.yaml build app
docker compose --env-file deployment.env -f compose.external.yaml run --rm migrate migrate --check
docker compose --env-file deployment.env -f compose.external.yaml run --rm migrate
docker compose --env-file deployment.env -f compose.external.yaml --profile queries up -d --wait
```

Migration is an explicit operation. Normal startup launches the runtime services and requires no administrator secret. The migration service belongs to the `tools` profile and is enabled when explicitly targeted. Application startup checks the catalog version and runtime role before serving requests. For upgrades, stop the application and workers first and follow the [catalog upgrade procedure](INSTALLATION.md#upgrade).

The HTTP port binds to localhost. Put a TLS reverse proxy and your organization's access controls in front of remote users. `/health` reports process liveness; `/ready` checks PostgreSQL readiness without model calls. Inspect container health and logs with the same environment and Compose arguments.

## Attach a table and run a query

For example, if the target database already contains `business.messages`, its owner or an administrator grants the runtime login access:

```sql
GRANT USAGE ON SCHEMA business TO semantic_runtime;
GRANT SELECT ON business.messages TO semantic_runtime;
```

Replace `semantic_runtime` with your `SDD_DB_USER`, and replace the schema and table with your source. Register it using the application container's configured runtime connection:

```bash
docker compose --env-file deployment.env -f compose.external.yaml exec app jev4pg attach messages --tenant demo --schema business --table messages
python deploy/configure.py --show-token
```

Open [the English workspace](http://127.0.0.1:8000/ask/en) or [简体中文](http://127.0.0.1:8000/ask/zh), connect with that workspace token, select `messages`, and run this in SQL mode:

```sql
SELECT COUNT(*) AS total FROM messages;
```

This reads the existing table without copying rows or calling a model. For natural-language questions, configure a [JEV provider](PROVIDERS.md), choose Natural language · JEV, and preview the generated SQL. Hybrid mode also needs [LLM settings](HYBRID_QUERY.md#configuration).

The generated token map and environment example use tenant `demo`; keep the attachment tenant, token map and `SDD_QUERY_TENANT` consistent if you change it. The `queries` profile above starts its background query worker; omit that profile if you only need interactive queries. Source grants and row-security policies control the visible population. Attachments are read-only through the workspace; see [source attachments](EXISTING_DATA.md) for column selection, views and tenant policies.

## Optional SQL clients

The `queries` profile above does not require a SQL extension. The separate `sql` profile below is for clients that submit asynchronous `jev.*` jobs from PostgreSQL.

Have the PostgreSQL administrator install the packaged extension files using [SQL access setup](POSTGRESQL_INTERFACE.md). Then stop the application, set `SDD_SQL_INTERFACE=1` in `deployment.env`, and run:

```bash
docker compose --env-file deployment.env -f compose.external.yaml run --rm migrate migrate --sql-interface
docker compose --env-file deployment.env -f compose.external.yaml --profile sql up -d --wait
```

The `sql` profile starts the worker alongside the application. Grant each SQL login its own tenant and actor mapping. SQL clients receive function execution rights; they do not receive the runtime catalog grants. Administrator secrets are mounted only into the migration service.

## Process-supervisor deployment

The same configuration works without containers. Install the package and run `jev4pg serve` under your process supervisor. Set `SDD_DB_SSLMODE=verify-full`, `SDD_DB_SSLROOTCERT` to the CA file accessible to that process, and the structured database credentials. `SDD_DB_SSLCERT` and `SDD_DB_SSLKEY` optionally select client certificate files. The administrator and runtime structured connections share these TLS settings.

An explicit `DATABASE_URL` or `DATABASE_URL_FILE` replaces the entire structured runtime connection configuration, including TLS options. Likewise, `SDD_ADMIN_DATABASE_URL` or its file variant replaces the structured administrator configuration. Put the required TLS parameters directly in an explicit URL. Password files and certificate-path settings serve different purposes: certificate settings contain paths, not PEM contents.

## Connection capacity

Size the pools against PostgreSQL's connection allowance before adding replicas:

| Setting | Default | Purpose |
| --- | --- | --- |
| `SDD_DB_POOL_SIZE` | 5 | Retained main connections per application or worker instance. |
| `SDD_DB_MAX_OVERFLOW` | 10 | Additional main connections during concurrent work. |
| `SDD_DB_GUARD_POOL_SIZE` | 2 | Separate shared connections that protect attached views during queries. No overflow. |
| `SDD_DB_POOL_TIMEOUT` | 30 seconds | Maximum wait for a free connection in either pool. |
| `SDD_DB_POOL_RECYCLE` | 1800 seconds | Connection age after which the next checkout reconnects. |
| `SDD_DB_CONNECT_TIMEOUT` | 10 seconds | Connection establishment timeout. |

One instance can open at most `POOL_SIZE + MAX_OVERFLOW + GUARD_POOL_SIZE` connections: 17 with defaults. The external environment example reduces this to 12. Multiply by all API and worker processes, then allow capacity for migrations, health-check processes, SQL clients and any native registry connections. Pools open connections as needed, not at construction.

Application query workers additionally create one metadata `Database` per concurrency slot for lease and cancellation traffic. Include each of those pools in the connection budget. At two slots, a worker process has the shared execution pools plus two metadata pool sets; their configured upper bound is 51 connections with defaults or 36 with the external example. Actual pools grow on demand. Lower pool settings when increasing worker replicas.

An attached-view query holds a guard connection before acquiring its main connection. Separate pools prevent concurrent guards from consuming all main slots while waiting for execution. Exhaustion returns a bounded checkout error; it does not open another pool or reinterpret missing work as false. Main-pool saturation can also delay SQL worker lease renewal, so reserve capacity for worker traffic when setting concurrency. Disposing an idle pool closes its connections; it does not cancel active queries.

Both pools check connection health at checkout. After a server disconnect, a later checkout replaces the failed idle connection. An interrupted transaction still fails; the pool does not replay it. This follows [SQLAlchemy's disconnect handling](https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic). Reconcile uncertain writes or provider calls before retrying them.

Pool exhaustion returns HTTP 503 with code `database_capacity`, without connection details. This describes the failure, not whether earlier work in the request completed. The response does not instruct clients to replay mutations automatically.

## Recovery and existing sources

Restore a database backup into an empty database with the original roles and matching extension files, then run migration and readiness checks before starting services. Keep the API, SQL workers and feature maintenance stopped while restoring and reviewing sources. Use the [backup procedure](INSTALLATION.md#backup-and-restore); test it in your own infrastructure.

Imported `sdd_data` tables and saved application state can be restored together. Source attachments also bind to cluster, database and runtime-role identity, so coincident relation OIDs cannot authorize a restored attachment. After restoring to another cluster, verify source ownership, row policies and data, then use a [reviewed manifest rebind](SOURCE_MANIFESTS.md#accept-schema-drift-or-a-restored-source). Do not rewrite stored identities to make validation pass. Logical restore and rebind are tested between independently initialized local clusters; cross-host recovery and high-availability qualification remain separate work.

## Development checks

`python deploy/check_compose.py` validates the resolved bundled, native and external service definitions without starting containers. `python deploy/verify_external.py --postgres-bin /path/to/postgresql17/bin` starts a disposable TLS server, tests bounded pools and certificate failures, and exercises an authenticated HTTP workspace through a database restart. Add `--full-suite` to run all Python regressions against that TLS server. It requires OpenSSL, the application dependencies and the SQL extension files in that PostgreSQL installation. Run it as a non-root user on Unix. It deletes only its own temporary files after stopping the test services.

These checks establish configuration and execution behavior. They do not certify a cloud provider, container host, high-availability topology or production workload capacity.
