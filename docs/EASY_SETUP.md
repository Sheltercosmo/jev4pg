# Python application on existing PostgreSQL

Install the Python application beside an existing PostgreSQL server. It supplies the workspace, natural-language planning and operator API. Its portable SQL interface lets SQL clients submit operators to a Python worker using ordinary PostgreSQL tables and functions. No native libraries, Rust compiler or PostgreSQL restart are required.

This guide installs current `main` for PostgreSQL 14–17. Follow all steps for queued operators, or complete the connection profile and migration, then jump to [the workspace](#add-the-workspace). Source tables stay in PostgreSQL. For synchronous SQL execution without a Python service, use the [native Rust extension](../native/README.md).

Table attachments and row-level security work on all four majors. Ordinary view attachments require PostgreSQL 15+; on 14, attach the underlying tables. [Version compatibility](INSTALLATION.md#postgresql-versions).

## Choose a path

| You want | Use |
| --- | --- |
| JEV operators from psql, DBeaver or a PostgreSQL driver | Portable SQL interface below. Submit a job and read its result. |
| Natural-language queries, data browsing and query history | Add the web workspace to the same installation. |
| Synchronous semantic functions inside PostgreSQL | [Native extension](../native/README.md). Requires an installation supported by your server. |
| A separate database to try the complete application | [Bundled Docker setup](INSTALLATION.md#docker-compose). |

The portable interface runs asynchronous jobs. Your server must permit schema creation, PL/pgSQL functions and the required role grants.

## 1. Create a connection profile

Use Python 3.11+ on the machine that will run the worker. Install from the current source archive:

```bash
python -m pip install https://github.com/Sheltercosmo/jev4pg/archive/refs/heads/main.zip
jev4pg setup
cd jev4pg-local
```

The wizard asks for the installation owner's PostgreSQL connection URL, a separate runtime login and password, and an optional TypeSafe key. Use your existing PostgreSQL 14–17 server; no container version selector is needed. Credentials are entered without terminal echo. For a new runtime login, leave its password blank to generate one. For an existing login, enter its current password; migration does not rotate it.

For a remote database, include `sslmode=verify-full` and configure a trusted CA, for example `sslrootcert=/path/to/ca.pem`. URL-encode special characters in URL passwords and paths. Local loopback connections can use the local server's TLS settings.

The new profile contains:

* `.env`: runtime configuration, with secret-file references.
* `admin.env`: the separate installation connection reference.
* `.secrets/`: credentials and a generated workspace token for tenant `demo`.

Setup creates local files only. It never connects to the database, overwrites an existing profile or prints credentials. Keep the directory private. Unix files are created with owner-only permissions; on Windows restrict the directory's ACL to the deployment account. Existing process environment values take precedence over profile values. Run the following commands from the profile directory so relative secret paths resolve correctly.

## 2. Install and grant access

Back up the target before its first migration. The installation owner needs permission to create the application schemas and their objects, create the restricted runtime role if absent, and assign the `sdd_data` schema to that role. A DBA can preprovision roles and their required memberships. The runtime login must be separate from the installation owner.

```bash
jev4pg --env-file admin.env migrate --check --sql-interface portable
jev4pg --env-file admin.env migrate --sql-interface portable
jev4pg --env-file admin.env sql-grant analyst --tenant demo --actor analyst
```

Replace `analyst` with an existing nonadministrative SQL login. The check is read-only. Migration creates the application catalog and the `jev` queue in one transaction. Repeating it preserves jobs and credentials. An unrelated `jev` schema or an incompatible installation stops setup rather than replacing objects.

The portable installation and `jevsd_pg` queue extension package have identical Python-backed `jev.*` APIs. Keep the mode originally used for that database. `--sql-interface` without a value selects the queue extension package; it does not install or enable the Rust `jev_native` engine.

## 3. Start the worker and try SQL

```bash
jev4pg sql-worker --heartbeat-file sql-worker.heartbeat
```

Keep this process running. For a deployment, use your normal process supervisor. The worker loads `.env`; it does not load `admin.env`. Configure an alternative or local [JEV provider](PROVIDERS.md) in the runtime profile if you left the key empty.

Connect to the same database as `analyst`. In psql:

```sql
SELECT jev.submit(
    'JEV.NOUL',
    '{"state":"The customer says the refund has not arrived.",
      "proposition":"The message needs follow-up."}',
    limits => '{"max_judgments":1,"max_requests":1}'
) AS job_id \gset

SELECT jsonb_pretty(jev.result(:'job_id'));
```

Poll the result after submitting and committing. Pending work is `NOT_EVALUATED`; uncertainty is `UNKNOWN`. Neither means false. A stopped worker leaves jobs queued. See [SQL usage and batching](POSTGRESQL_INTERFACE.md) for all operators, cancellation and authorization.

For an existing `public.tickets` table, PostgreSQL can prepare the context directly:

```sql
SELECT jev.submit(
    'JEV.NOUL',
    jsonb_build_object('state', row_to_json(t), 'proposition', 'This ticket needs follow-up.'),
    limits => '{"max_judgments":1,"max_requests":1}'
)
FROM (SELECT id, body FROM public.tickets ORDER BY id LIMIT 5) AS t;
```

The SQL login needs source SELECT permission. This bounded example submits five independent jobs; for larger populations use the documented batch operators or native scans. Selected context is sent to the configured JEV provider. The queue is capped at 100 active jobs per tenant, and each request at 512 KiB.

## Add the workspace

In another terminal, from the same profile directory:

```bash
jev4pg serve
```

Open `http://127.0.0.1:8000/ask/en` or `/ask/zh`. Read the generated token locally from `.secrets/api-tokens.json` and enter it in the workspace. To use existing tables, give the runtime login appropriate PostgreSQL grants and [attach them without copying rows](EXISTING_DATA.md). For example:

```bash
jev4pg attach tickets --tenant demo --schema public --table tickets
```

Natural-language planning uses JEV; hybrid planning also needs [LLM configuration](HYBRID_QUERY.md). Background workspace queries additionally use `jev4pg query-worker --tenant demo`. SQL queue jobs and workspace query jobs are distinct workers.

## Updates and backups

Use the same application build for the API, workers and migration. Stop workers during upgrades. Follow [application migration and backup procedures](INSTALLATION.md#upgrade). Portable SQL objects, jobs, role mappings and the installation marker are ordinary database objects included in a full `pg_dump`; they do not require extension control files during restore. Restore into an empty database with the original roles. Do not remove the `jev` schema as an uninstall shortcut: it contains queued jobs and results.

The installer does not alter business-table ownership or public-schema permissions. Provider keys remain in worker configuration, outside SQL text and query history.
