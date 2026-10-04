# Native Compose deployment

The native stack builds an optimized Rust extension into PostgreSQL 17 and enables it for the query API. It also creates a dedicated evidence-registry login. Python provides planning, the workspace and queued operators; native semantic reads and probability embeddings execute inside PostgreSQL.

This deployment uses application 0.7.0.dev0, catalog schema 2 and native extension 0.2.0. It is a development preview and does not enable native maintained-feature refresh or semantic write review.

## Start

Use Docker Compose v2 and Python 3.11+. Clone the development source, then configure and start it:

```bash
git clone https://github.com/Sheltercosmo/jevsd-pg.git
cd jevsd-pg
python deploy/configure.py --native
docker compose -f compose.yaml -f compose.native.yaml build
docker compose -f compose.yaml -f compose.native.yaml up -d --wait
```

For an existing checkout, run the configuration and Compose commands from its root. The first build compiles Rust and pgrx. Later builds can reuse Docker's build cache. The runtime PostgreSQL image contains native extension 0.2.0 and its SQL files; it does not contain the Rust toolchain.

The native Compose project is named `jevsd-pg-native`, with a separate database volume from the default stack. Both use the same default host ports. Set `SDD_HTTP_PORT` and `SDD_POSTGRES_PORT` in `.env` when running them together. Use the same two Compose files for later commands.

Open `/ask/en` or `/ask/zh` on the configured HTTP port. Retrieve the workspace token with `python deploy/configure.py --show-token`. The application manifest identifies native semantic execution as `rust_postgresql`.

## Providers and credentials

The native PostgreSQL process reads the same `SDD_JEV_ENDPOINT`, `SDD_JEV_MODEL` and `SDD_JEV_REVISION` settings as the application. Configure a compatible HTTP endpoint through `.env`; a local model must expose the [SystemOne contract](PROVIDERS.md#request-and-response-contract). In-process Python adapters are not a native transport.

At startup, a shell entrypoint creates a PostgreSQL-owned configuration file under `/run/jev`. Provider keys come from mounted secret files, not Docker build arguments or SQL. An empty custom key falls back to the TypeSafe key only for the default TypeSafe endpoint. Changing configuration requires restarting PostgreSQL and the application.

`configure.py --native` adds `native_registry_password` without rotating existing credentials. Migration creates `jev_registry` with only the three registry function grants and a connection limit of eight. It rejects an existing registry role with other role memberships, administrative privileges or source-table access. Existing passwords are preserved; the file must match the existing role when adopting a database.

The registry uses a loopback connection to this PostgreSQL server. Defaults allow 16 active provider requests and 10,000 admissions per scope and day, with one-day evidence age. Application query and daily budgets remain additional limits. A scan can require one registry connection in addition to its source connection; allow capacity for both.

For a custom registry policy, mount a complete provider JSON file and set `JEV_NATIVE_CONFIG_FILE` in the PostgreSQL container environment. The entrypoint preserves an explicitly supplied configuration. Follow the [registry guide](NATIVE_EVIDENCE.md) for fields and access rules.

## SQL clients

The application receives native execution grants during migration. Give other PostgreSQL clients only the required source and function privileges, following [native SQL setup](../native/README.md#configure). The asynchronous `jev.*` interface and its worker remain available alongside `jev_native.*`.

Run [probability embeddings](NATIVE_EMBEDDINGS.md) or [stage plans](NATIVE_PLANS.md) directly from a SQL client. No Python service is needed for these calls. Exact SQL filtering and projection precede native evaluation where declared by the source query; independent provider work keeps the shared scheduler.

## Updates and backups

Stop application traffic and back up before changing images. Native extension 0.2.0 registers evidence, request attempts, admission counters and their sequences for PostgreSQL dumps. Earlier 0.1.0 dumps omit this registry state. Upgrade the extension before relying on a new dump for recovery.

```bash
docker compose -f compose.yaml -f compose.native.yaml stop app sql-worker
docker compose -f compose.yaml -f compose.native.yaml build
docker compose -f compose.yaml -f compose.native.yaml up -d --wait postgres
docker compose -f compose.yaml -f compose.native.yaml run --rm migrate
docker compose -f compose.yaml -f compose.native.yaml up -d --wait
```

Migration upgrades an installed native extension from 0.1.0 to 0.2.0 transactionally and preserves stored data and grants. It rejects other mismatched versions. Restart PostgreSQL when replacing the shared library so existing backends cannot retain an older binary. Native extension updates need installation files and the matching library on the server.

Use the [backup commands](INSTALLATION.md#backup-and-restore) with `-f compose.yaml -f compose.native.yaml` added after `docker compose`. Restore into an empty database on a server with the matching extension files and original roles. Save role credentials and secret files separately. Restored vectors can be compared locally; registry evidence remains historical data. A new database or cluster identity prevents it from being silently reused as a new source's observation.

Source attachments pin physical relation identities. If restoration changes those identities, verify the restored relations and detach and register them again before application queries. This preserves source rows and makes the new binding explicit; old approvals must not silently authorize a replacement relation.

Unfinished request attempts retain their state and admission counters after restore. Inspect them before reopening traffic. Do not clear uncertain attempts to obtain automatic retries; follow [reconciliation](NATIVE_EVIDENCE.md).

## Validation

The [native deployment workflow](../.github/workflows/native-deployment.yml) builds the optimized image and runs disposable HTTP and PostgreSQL checks. It covers migration, restricted access, Chinese text, duplicate and NULL handling, probability storage, restart reuse and backup restoration. Its synthetic provider verifies the execution contract, not model quality or production throughput.
