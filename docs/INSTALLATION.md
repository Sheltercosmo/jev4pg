# Installation

Start with the [quick start](../README.md#quick-start) for a first query. This guide covers deployment, credentials and upgrades. PostgreSQL owns storage and relational execution in every setup; Python supplies the application, and Rust supplies the optional native semantic engine.

## Choose a deployment

| Use case | Install | Guide |
| --- | --- | --- |
| Try the workspace, APIs and natural-language queries | Python application with bundled PostgreSQL and workers | [Docker Compose](#docker-compose) |
| Add the application or queued operators to an existing server | Python package beside PostgreSQL 14–17; no native server files | [Guided setup](EASY_SETUP.md), or [external Compose](EXTERNAL_POSTGRESQL.md) |
| Call synchronous semantic functions from SQL | Rust extension on PostgreSQL 14–17 Linux; no Python service | [Native build and configuration](../native/README.md#build) |
| Use the workspace with native semantic reads | Python application plus Rust extension | [Native Compose deployment](NATIVE_DEPLOYMENT.md) |

The default Compose stack uses the Python semantic engine. Native execution is an explicit choice, not a requirement for the application. Maintained semantic features and semantic write review use the Python engine. See the [capability map](CAPABILITIES.md#python-and-native-roles) for interface boundaries and use cases.

The released `v0.8.0` checkout includes native preview 0.3.0; current `main` includes native preview 0.4.0. Use a release tag for a fixed deployment and keep all components on that checkout. See [version compatibility](#upgrade) before changing an existing installation.

## PostgreSQL versions

Current `main` supports PostgreSQL 14, 15, 16 and 17 for the Python application, queued SQL interface and native Rust extension. The published v0.8.0 deployment remains configured for PostgreSQL 17. Use `main` for the version selector and older-major native builds.

| Capability | PostgreSQL 14 | PostgreSQL 15–17 |
| --- | --- | --- |
| Application, operators and queued SQL | Supported | Supported |
| Native scans, stage plans and embeddings | Supported on Linux | Supported on Linux |
| Table and partitioned-table attachments, including row security | Supported | Supported |
| Ordinary view attachments | Attach the underlying tables instead | Require `security_invoker=true` throughout the view chain |
| Materialized-view attachments | Read the stored snapshot | Read the stored snapshot |

The experimental remote-source protocol remains qualified on PostgreSQL 17 only. It validates stored PostgreSQL expression layouts and is separate from local source attachments.

For a new PostgreSQL 14 deployment, clone current development:

```bash
git clone --branch main https://github.com/Sheltercosmo/jev4pg.git
cd jev4pg
python deploy/configure.py
```

Add `SDD_POSTGRES_MAJOR=14` to the checkout's `.env`, then run `docker compose build` and `docker compose up -d --wait`. Choose 14, 15, 16 or 17; the default is 17. For native execution, configure with `--native` and use both Compose files as in the [combined deployment guide](NATIVE_DEPLOYMENT.md). The same selector controls headers, library paths and the Rust feature for that major.

For an existing PostgreSQL 14–17 server, install the application from this checkout (`python -m pip install .`), then follow [guided setup](EASY_SETUP.md) starting at `jev4pg setup`. No server version selector is needed for an external connection.

Changing `SDD_POSTGRES_MAJOR` does not upgrade a database volume. Keep it fixed for an existing deployment. To change majors, use PostgreSQL's `pg_upgrade` or a dump/restore into a new volume, install the matching extension binary and validate the application before switching traffic. Never mount an old-major data directory under a different major's image.

## Docker Compose

Install Docker with Compose v2 and Python 3.11 or newer. Use the `v0.8.0` tag for the released deployment, or `main` to develop the application. From that checkout:

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

The stack starts PostgreSQL, runs an idempotent migration, then starts the API, SQL worker and application query worker. Source data, evidence and queued jobs live in the `sdd_pg` volume. The API and worker run as an unprivileged user with a read-only container filesystem. Administrator credentials are available only to PostgreSQL and migration.

The query worker serves the generated `demo` tenant. If you change the API token mapping, set `SDD_QUERY_TENANT` to the matching tenant in `.env`. See [background query deployment](QUERY_JOBS.md#start-a-worker) for concurrency and multiple tenants.

HTTP and PostgreSQL bind to localhost. Set `SDD_HTTP_PORT` or `SDD_POSTGRES_PORT` in `.env` to change their host ports. For remote access, place the API behind a TLS reverse proxy and configure firewall and PostgreSQL TLS rules for your environment. Keep `.secrets/` private; on Windows restrict its inherited file permissions to the deployment account.

Use `docker compose ps` to inspect health. `/health` is process liveness; `/ready` checks database connectivity, schema version and runtime permissions. Neither endpoint calls a model or verifies provider availability.

## Existing PostgreSQL server

For an application-only deployment with verified TLS and explicit migration control, follow [external PostgreSQL deployment](EXTERNAL_POSTGRESQL.md). It includes connection sizing and an optional SQL worker. The process-supervisor installation below remains available.

Use PostgreSQL 14–17 with current development and a dedicated database. Install the Python package:

```bash
python -m venv .venv
# Bash: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps .
```

Copy `.env.example` to `.env` and configure the runtime database URL, provider and API token map. For setup, provide `SDD_ADMIN_DATABASE_URL` and `SDD_DB_PASSWORD` or their `_FILE` variants. The administrator must be able to create roles and schema objects. The new runtime password must contain at least 24 characters.

For a new installation, use the portable queue interface:

```bash
jev4pg migrate --check --sql-interface portable
jev4pg migrate --sql-interface portable
```

This creates the restricted runtime login, application tables, tenant policies, immutable evidence guards and the `jev.*` job queue as ordinary PostgreSQL objects. It runs in one transaction and can be repeated. Existing runtime passwords are preserved. No custom extension files are required.

An existing installation using the `jevsd_pg` queue package should retain that mode: export files with `jev4pg extension-files ./extension-files`, copy them to the server's `pg_config --sharedir` directory under `extension/`, and run `jev4pg migrate --sql-interface extension`. This packages the same Python-backed queue as a PostgreSQL extension. It is separate from the Rust `jev_native` extension.

The read-only `migrate --check` command validates installation ownership, supported catalog versions, table contracts, tenant policies and runtime-role memberships. It returns JSON and exits nonzero on a conflict. It neither calls a model nor creates roles. It does not test extension availability, provider connectivity or backup recovery; use the matching installation procedure and readiness checks for those.

The same check runs inside migration before any installation changes. Catalog version 4 keeps metadata, evidence, history and application query jobs in `sdd_catalog`, separate from imported rows in `sdd_data`. Runtime statements name the catalog explicitly; custom search paths and temporary tables cannot redirect them. Installation leaves business tables, functions and the `public` schema's grants unchanged. Source access still requires explicit PostgreSQL grants and attachment registration.

An unmanaged `sdd_catalog` or `sdd_data` schema stops installation. A legacy `public.sdd_schema_version` marker must describe a supported version 1 or 2 installation before migration can move its objects. Conflicting markers, owners or tenant policies stop the upgrade. Inspect the reported objects; do not delete them to make the check pass. Catalog isolation is a deployment foundation, not a claim of tested shared-database capacity or availability.

Remove administrator credentials from the service environment. Set `SDD_ENV=production`, `SDD_SQL_INTERFACE=1`, and start the services under your process supervisor:

```bash
jev4pg serve --host 127.0.0.1 --port 8000
jev4pg sql-worker --concurrency 2 --heartbeat-file /tmp/jev-sql-worker.heartbeat
```

Run these as separate processes. On Windows choose a writable heartbeat path, such as `.runtime/sql-worker.heartbeat`. Production startup rejects SQLite, administrative runtime roles, missing migration state and invalid API token mappings. Use a separate PostgreSQL login for each SQL client; follow [SQL access setup](POSTGRESQL_INTERFACE.md#grant-access).

## Native preview

The [native Compose guide](NATIVE_DEPLOYMENT.md) installs the combined application and native engine. Its release commands use `v0.8.0` with native 0.3.0; use one `main` checkout for all components when evaluating native 0.4.0 development.

For SQL-only use on an existing server, follow the [native build guide](../native/README.md#build), configure `JEV_NATIVE_CONFIG_FILE` in the PostgreSQL server environment and grant SQL callers access. Direct SQL use does not require the Python application, its catalog or its workers.

For application integration, install Python from that same checkout and run:

```bash
jev4pg migrate --native-interface
```

Set `SDD_SEMANTIC_ENGINE=native` in the application environment and start the service. This enables supported semantic reads through `/ask` and `/data/sql`. It does not switch all operators to Rust. Maintained semantic features and semantic mutation review require the default Python engine. The [roadmap](IMPLEMENTATION_PLAN.md) lists remaining release work; use a separate database for preview evaluation.

The application still handles planning, authentication, dataset registration, history and review. `POST /jev/call` and queued `jev.*` operators still use Python. Choose JEV or hybrid planning separately from the semantic execution engine.

The application and native extension configure providers separately. The Rust executor calls a compatible HTTP endpoint; a local Python adapter needs an HTTP wrapper to serve it. See [providers](PROVIDERS.md) and [native configuration](../native/README.md#configure).

## Existing source data

The application can register authorized PostgreSQL tables and views without importing their rows. Follow [source attachments](EXISTING_DATA.md) after the matching migration. Attachments are read-only through the workspace and preserve source ownership and PostgreSQL permissions.

## Credentials and providers

A runtime connection can use `DATABASE_URL`, or `SDD_DB_HOST`, `SDD_DB_PORT`, `SDD_DB_NAME`, `SDD_DB_USER` and `SDD_DB_PASSWORD_FILE`. Structured settings handle special characters in passwords. Setup uses `SDD_ADMIN_DATABASE_URL` or `SDD_ADMIN_DB_USER` with `SDD_ADMIN_DB_PASSWORD_FILE` and the same host/database settings.

`SDD_API_TOKENS_FILE` contains a JSON map from random tokens to tenant, name and role. Roles are `reader` and `reviewer`; production tokens need at least 32 characters. Provider secrets support `TYPESAFE_API_KEY_FILE`, `SDD_JEV_API_KEY_FILE` and `OPENAI_API_KEY_FILE`. A file setting takes precedence over its corresponding value.

Compose reads provider settings from `.env` and mounts key files from `.secrets/`. It does not pass the entire `.env` to services. Edit `typesafe_api_key`, `jev_api_key` or `llm_api_key` for the selected provider. Restart the API and both workers after changing credentials. Configure `SDD_JEV_ENDPOINT`, model and revision for a compatible HTTP endpoint. An in-process Python adapter requires a derived app image containing that adapter; see [providers](PROVIDERS.md).

Hybrid queries require `SDD_LLM_TRANSPORT`, `SDD_LLM_MODEL` and the corresponding LLM credential. See [hybrid setup](HYBRID_QUERY.md).

## Upgrade

Choose a release tag or a development commit first. The application number identifies that build. Catalog and extension numbers describe database upgrade formats; they are not competing project releases. `main` changes over time, so record its commit SHA when deploying a development build.

Application, catalog and extension compatibility:

| Source | Application | Catalog schema | Optional native extension |
| --- | --- | --- | --- |
| Released `v0.6.0` tag | 0.6.0 | 1 | Not included |
| Released `v0.7.0` tag | 0.7.0 | 4 | 0.2.0 preview |
| Released `v0.8.0` tag | 0.8.0 | 4 | 0.3.0 preview |
| Development checkout | 0.8.0 | 4 | 0.4.0 preview |

The Python distribution was renamed from `jevsd-pg` to `jev4pg`. For installations outside Compose, create a fresh virtual environment and install the new wheel or source checkout there; do not install both distributions over one another. They share the `sdd` import namespace. Point the existing service supervisor at the new environment after migration. The `jevsd-pg` and `sdd` command aliases remain available.

The `jev4pg` command is available from v0.7.0; use `jevsd-pg` with the unchanged v0.6.0 package. The Python import namespace `sdd`, queue extension package `jevsd_pg`, configuration keys and Compose identities retain their existing names so upgrades preserve installed databases.

Back up the database and retain its role credentials first. Check out the desired release, then run:

```bash
docker compose stop app sql-worker query-worker
docker compose build
docker compose run --rm migrate
docker compose up -d --wait
```

Stop the API, workers and administrative writes before upgrading. Catalog version 4 adds the tenant-scoped application query job table to a version 3 installation. Upgrades from version 1 or 2 also move validated catalog tables and guard functions from `public` into `sdd_catalog` in one transaction. PostgreSQL retains existing identities, rows, indexes, constraints and grants. Imported source tables, existing attachments and the `jev` operator queue remain in their original schemas. Passwords are preserved. A failure rolls back the migration; an already committed upgrade has no automatic downgrade. Restore the pre-upgrade backup with the old application if rollback is required.

Run the API and workers from the same application version after upgrading. Processes that expect the public catalog or version 3 cannot serve the version 4 lifecycle. Update any administrator scripts that directly query internal tables to use `sdd_catalog`. Do not add compatibility views in `public`. Never run two migration versions against the same database at once. Test migration and restore on a database copy before deployment, and do not repoint Compose at an unrelated database volume. A legacy upgrade does not undo public-schema grants changed by earlier releases; the administrator remains responsible for those grants. Configure application query workers using the [query job guide](QUERY_JOBS.md).

Review pending writes again after upgrading. Ordinary mutation previews created by v0.6.0 use the former snapshot contract and are rejected as stale by the bounded write executor. Their SQL remains available for inspection. Generate a new preview and approve its current targets before committing; migration does not apply those writes automatically.

Check the installed application version with `jev4pg --version`, or `docker compose exec app jev4pg --version`. `/health` and the OpenAPI document report the same application version. Inspect database versions as an administrator:

```sql
SELECT version FROM sdd_catalog.sdd_schema_version;
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('jevsd_pg', 'jev_native');
```

For an installation that has not yet upgraded from catalog version 1 or 2, query `public.sdd_schema_version` instead.

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

The deployment and [native workflows](../.github/workflows/native.yml) run a PostgreSQL 14–17 matrix. Native verification installs the published v0.6.0 application in isolation, creates a populated database and upgrades it on each major. Checks cover rollback, repeatability, data and password preservation, reviewed features, evidence reuse, query history, job states and tenant isolation. These are application and extension upgrades within each PostgreSQL major; PostgreSQL major-version migration remains a separate procedure.
