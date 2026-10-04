# Installation

Choose the application deployment or the native execution preview:

| Path | Requirements | Execution |
| --- | --- | --- |
| Released application, v0.6.0 | Python 3.11+, PostgreSQL 17 or Docker Compose v2 | Python service and worker; asynchronous `jev.*` SQL jobs. |
| Development application, 0.7.0.dev0 | Same application requirements; optional native build below | Current catalog and query service, including source attachments. |
| Native extension, 0.2.0 preview | Docker Compose, or PostgreSQL 17 on Linux with Rust 1.96, pgrx 0.19.2 and build headers | Synchronous `jev_native.*` functions inside PostgreSQL; optional application integration. |

The default Compose stack does not include the Rust extension. PostgreSQL performs storage, joins, arithmetic and transactions in both paths.

## Docker Compose

Install Docker with Compose v2 and Python 3.11 or newer. Use the `v0.6.0` tag for the released deployment, or `main` to develop the application. From that checkout:

```bash
python deploy/configure.py
docker compose build
docker compose up -d --wait
```

The configuration command creates `.secrets/` and asks for a TypeSafe key. It preserves existing files when rerun. For another provider, leave that prompt empty and follow [provider configuration](PROVIDERS.md). Provider keys are separate from the workspace token.

Open [English](http://127.0.0.1:8000/ask/en) or [简体中文](http://127.0.0.1:8000/ask/zh). Retrieve your workspace token locally:

```bash
python deploy/configure.py --show-token
```

The stack starts PostgreSQL, runs an idempotent migration, then starts the API and SQL worker. Source data, evidence and queued jobs live in the `sdd_pg` volume. The API and worker run as an unprivileged user with a read-only container filesystem. Administrator credentials are available only to PostgreSQL and migration.

HTTP and PostgreSQL bind to localhost. Set `SDD_HTTP_PORT` or `SDD_POSTGRES_PORT` in `.env` to change their host ports. For remote access, place the API behind a TLS reverse proxy and configure firewall and PostgreSQL TLS rules for your environment. Keep `.secrets/` private; on Windows restrict its inherited file permissions to the deployment account.

Use `docker compose ps` to inspect health. `/health` is process liveness; `/ready` checks database connectivity, schema version and runtime permissions. Neither endpoint calls a model or verifies provider availability.

## Existing PostgreSQL server

Use PostgreSQL 17 and a dedicated database. Install the Python package:

```bash
python -m venv .venv
# Bash: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps .
```

Copy `.env.example` to `.env` and configure the runtime database URL, provider and API token map. For setup, provide `SDD_ADMIN_DATABASE_URL` and `SDD_DB_PASSWORD` or their `_FILE` variants. The administrator must be able to create roles and schema objects. The new runtime password must contain at least 24 characters.

For the SQL interface, export the packaged extension files:

```bash
jevsd-pg extension-files ./extension-files
```

On the PostgreSQL server, copy the resulting `.control` and `.sql` files into the directory printed by `pg_config --sharedir`, under `extension/`. Then run:

```bash
jevsd-pg migrate --sql-interface
```

This creates the restricted runtime login, application tables, tenant policies, immutable evidence guards and `CREATE EXTENSION jevsd_pg`. It runs in one transaction and can be repeated. Existing runtime passwords are preserved. Managed PostgreSQL services that disallow custom extension files can use `jevsd-pg migrate` for the HTTP interface; the SQL interface requires extension installation access.

Remove administrator credentials from the service environment. Set `SDD_ENV=production`, `SDD_SQL_INTERFACE=1`, and start the services under your process supervisor:

```bash
jevsd-pg serve --host 127.0.0.1 --port 8000
jevsd-pg sql-worker --concurrency 2 --heartbeat-file /tmp/jev-sql-worker.heartbeat
```

Run these as separate processes. On Windows choose a writable heartbeat path, such as `.runtime/sql-worker.heartbeat`. Production startup rejects SQLite, administrative runtime roles, missing migration state and invalid API token mappings. Use a separate PostgreSQL login for each SQL client; follow [SQL access setup](POSTGRESQL_INTERFACE.md#grant-access).

## Native preview

Use a checkout of `main`. The [native Compose guide](NATIVE_DEPLOYMENT.md) builds the extension and configures its evidence registry with mounted credentials. For an existing PostgreSQL server, follow the [native build guide](../native/README.md#build), configure `JEV_NATIVE_CONFIG_FILE` in the server environment and grant SQL callers access. Direct SQL use does not require the Python application.

For application integration, install Python from that same checkout and run:

```bash
jevsd-pg migrate --native-interface
```

Set `SDD_SEMANTIC_ENGINE=native` in the application environment and start the service. This enables supported semantic reads through `/ask` and `/data/sql`. It does not switch all operators to Rust. Maintained semantic features and semantic mutation review require the default Python engine. The [roadmap](IMPLEMENTATION_PLAN.md) lists remaining release work; use a separate database for preview evaluation.

The application and native extension configure providers separately. The Rust executor calls a compatible HTTP endpoint; a local Python adapter needs an HTTP wrapper to serve it. See [providers](PROVIDERS.md) and [native configuration](../native/README.md#configure).

## Existing source data

The development application can register authorized PostgreSQL tables and views without importing their rows. Follow [source attachments](EXISTING_DATA.md) after the matching migration. Attachments are read-only through the workspace and preserve source ownership and PostgreSQL permissions.

## Credentials and providers

A runtime connection can use `DATABASE_URL`, or `SDD_DB_HOST`, `SDD_DB_PORT`, `SDD_DB_NAME`, `SDD_DB_USER` and `SDD_DB_PASSWORD_FILE`. Structured settings handle special characters in passwords. Setup uses `SDD_ADMIN_DATABASE_URL` or `SDD_ADMIN_DB_USER` with `SDD_ADMIN_DB_PASSWORD_FILE` and the same host/database settings.

`SDD_API_TOKENS_FILE` contains a JSON map from random tokens to tenant, name and role. Roles are `reader` and `reviewer`; production tokens need at least 32 characters. Provider secrets support `TYPESAFE_API_KEY_FILE`, `SDD_JEV_API_KEY_FILE` and `OPENAI_API_KEY_FILE`. A file setting takes precedence over its corresponding value.

Compose reads provider settings from `.env` and mounts key files from `.secrets/`. It does not pass the entire `.env` to services. Edit `typesafe_api_key`, `jev_api_key` or `llm_api_key` for the selected provider. Restart the API and SQL worker after changing credentials. Configure `SDD_JEV_ENDPOINT`, model and revision for a compatible HTTP endpoint. An in-process Python adapter requires a derived app image containing that adapter; see [providers](PROVIDERS.md).

Hybrid queries require `SDD_LLM_TRANSPORT`, `SDD_LLM_MODEL` and the corresponding LLM credential. See [hybrid setup](HYBRID_QUERY.md).

## Upgrade

Choose a release tag or a development commit first. The application number identifies that build. Catalog and extension numbers describe database upgrade formats; they are not competing project releases. `main` changes over time, so record its commit SHA when deploying a development build.

Application, catalog and extension compatibility:

| Source | Application | Catalog schema | Optional native extension |
| --- | --- | --- | --- |
| Released `v0.6.0` tag | 0.6.0 | 1 | Not included |
| Development `main` | 0.7.0.dev0 | 2 | 0.2.0 preview |

Back up the database and retain its role credentials first. Check out the desired release, then run:

```bash
docker compose stop app sql-worker
docker compose build
docker compose run --rm migrate
docker compose up -d --wait
```

The current migration is additive and preserves source data and evidence. It does not rotate passwords. Never run two migration versions against the same database at once. Before adopting this deployment on an existing installation, test migration and restore on a database copy. Do not repoint Compose at an unrelated database volume.

On a development build, check the installed application version with `jevsd-pg --version`, or `docker compose exec app jevsd-pg --version`. `/health` and the OpenAPI document report the same application version. Inspect database versions as an administrator:

```sql
SELECT version FROM sdd_schema_version;
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('jevsd_pg', 'jev_native');
```

Use the [native update procedure](NATIVE_DEPLOYMENT.md#updates-and-backups) when the Rust extension is installed. The default and native Compose projects use separate volumes; starting the native project does not upgrade an existing default project.

## Backup and restore

The database contains datasets, evidence, role mappings and SQL jobs. Save a custom-format backup without shell binary redirection:

```bash
docker compose exec postgres pg_dump -U sdd_admin -d sdd -Fc -f /tmp/sdd.dump
docker compose cp postgres:/tmp/sdd.dump ./sdd.dump
```

Back up PostgreSQL login roles and `.secrets/` separately in protected storage; database dumps do not include cluster roles. To test restoration into a fresh database on the same server:

```bash
docker compose exec postgres createdb -U sdd_admin sdd_restore
docker compose cp ./sdd.dump postgres:/tmp/sdd.dump
docker compose exec postgres pg_restore -U sdd_admin -d sdd_restore --exit-on-error /tmp/sdd.dump
```

On a new server, install the extension files and recreate the original roles first. Restore into an empty database, without running migration first. Then run the matching migration and readiness checks. Keep API and workers stopped until the restored database has been checked. Jobs running at backup time can expire after restore; inspect their operator records before resubmitting.

`docker compose down` preserves the database volume. `docker compose down -v` deletes it.

## Deployment tests

The [deployment workflow](../.github/workflows/deployment.yml) builds the images and checks fresh installation, SQL authorization, worker recovery, HTTP and `psql` calls, batching, repeat migration, restart persistence and backup restoration. Its explicit synthetic provider tests integration, not language accuracy. The default stack never enables this fixture.

The [native workflow](../.github/workflows/native.yml) also installs the published v0.6.0 package into an isolated environment, creates a populated database and upgrades it on PostgreSQL 17. It checks migration rollback and repeatability, unchanged data and passwords, reviewed features, evidence reuse, query history, job states and tenant isolation. Native reads and source attachments are exercised after migration. This covers the application upgrade on one PostgreSQL version; it does not establish a PostgreSQL major-version or cross-host migration procedure.
