# Combined application and native deployment

The native stack builds an optimized Rust extension into the selected PostgreSQL major (17 by default) and enables it for the query API. It also creates a dedicated evidence-registry login. Python provides planning, the workspace and queued operators; native semantic reads and probability embeddings execute inside PostgreSQL.

Use this deployment when you want the workspace and natural-language planning with native semantic reads. For SQL-only use without the Python application, follow the [native build guide](../native/README.md#build). For maintained semantic features or semantic write review, use the [Python application deployment](INSTALLATION.md#docker-compose).

The release commands below use application v0.8.0, catalog schema 4 and native preview 0.3.0. Current `main` uses native preview 0.4.0. Build PostgreSQL, the application and workers from the same checkout; see [component compatibility](INSTALLATION.md#upgrade).

For PostgreSQL 14, 15 or 16, clone `main` and set `SDD_POSTGRES_MAJOR` in `.env` before building. Current development supports all four majors through 17; the release commands below retain PostgreSQL 17. See [version selection and data-volume upgrades](INSTALLATION.md#postgresql-versions).

## Start

Use Docker Compose v2 and Python 3.11+. Clone the tagged source, then configure and start it:

```bash
git clone --branch v0.8.0 https://github.com/Sheltercosmo/jev4pg.git
cd jev4pg
python deploy/configure.py --native
docker compose -f compose.yaml -f compose.native.yaml build
docker compose -f compose.yaml -f compose.native.yaml up -d --wait
```

For an existing checkout, run the configuration and Compose commands from its root. To evaluate current development, clone `--branch main` instead of `--branch v0.8.0`. The first build compiles Rust and pgrx. Later builds can reuse Docker's build cache. The runtime PostgreSQL image contains that checkout's native library and SQL files; it does not contain the Rust toolchain.

The native Compose project is named `jevsd-pg-native`, with a separate database volume from the default stack. Both use the same default host ports. Set `SDD_HTTP_PORT` and `SDD_POSTGRES_PORT` in `.env` when running them together. Use the same two Compose files for later commands.

Open `/ask/en` or `/ask/zh` on the configured HTTP port. Retrieve the workspace token with `python deploy/configure.py --show-token`. The application manifest identifies native semantic execution as `rust_postgresql`.

## Component responsibilities

| Component | Role |
| --- | --- |
| Python API | Workspace, JEV or hybrid planning, catalog, history, review and the 41-operator API. |
| Native Rust engine | Supported semantic read stages, parallel requests, evidence reuse and probability embeddings. |
| PostgreSQL | Source reads, permissions, relational SQL, transactions and persisted results. |
| Python SQL worker | Asynchronous operators submitted through `jev.*`; separate from direct `jev_native.*` calls. |
| Python query worker | Background application queries, using the configured native semantic engine. |

The overlay sets `SDD_SEMANTIC_ENGINE=native` for the API and query worker and disables maintained-feature refresh. It does not replace the Python operator runtime or choose a planning mode. Select JEV or Hybrid in the workspace independently; Hybrid needs its own LLM configuration.

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
docker compose -f compose.yaml -f compose.native.yaml stop app sql-worker query-worker
docker compose -f compose.yaml -f compose.native.yaml build
docker compose -f compose.yaml -f compose.native.yaml up -d --wait postgres
docker compose -f compose.yaml -f compose.native.yaml run --rm migrate
docker compose -f compose.yaml -f compose.native.yaml up -d --wait
```

Migration targets the native version in the selected checkout. Release v0.8.0 upgrades native 0.1.0 or 0.2.0 to 0.3.0; current development upgrades 0.1.0, 0.2.0 or 0.3.0 to 0.4.0. These updates are transactional and preserve stored data and grants. Other mismatched versions are rejected. Restart PostgreSQL when replacing the shared library so existing backends cannot retain an older binary. Native extension updates need installation files and the matching library on the server.

Use the [backup commands](INSTALLATION.md#backup-and-restore) with `-f compose.yaml -f compose.native.yaml` added after `docker compose`. Restore into an empty database on a server with the matching extension files and original roles. Save role credentials and secret files separately. Restored vectors can be compared locally; registry evidence remains historical data. A new database or cluster identity prevents it from being silently reused as a new source's observation.

Source attachments pin physical relation identities. If restoration changes those identities, verify the restored relations and detach and register them again before application queries. This preserves source rows and makes the new binding explicit; old approvals must not silently authorize a replacement relation.

Unfinished request attempts retain their state and admission counters after restore. Inspect them before reopening traffic. Do not clear uncertain attempts to obtain automatic retries; follow [reconciliation](NATIVE_EVIDENCE.md).

## Validation

The [native deployment workflow](../.github/workflows/native-deployment.yml) builds the optimized image and runs disposable HTTP and PostgreSQL checks. It covers migration, restricted access, Chinese text, duplicate and NULL handling, probability storage, restart reuse and backup restoration. Its synthetic provider verifies the execution contract, not model quality or production throughput.
