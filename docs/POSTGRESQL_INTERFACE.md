# Queued PostgreSQL interface

This guide covers the `jevsd_pg` extension and asynchronous `jev.*` functions. For direct Rust execution, use the separate [native extension](../native/README.md), with `jev_native.scan`, stage plans and embeddings.

Run JEV operators from `psql` or any PostgreSQL driver. `jev.submit` queues an operator, `jev.result` reads its result, and `jev.cancel` cancels a queued request. The Python worker executes the same operator implementation used by the HTTP API.

The interface is asynchronous. Submit and commit before polling; a worker cannot see an uncommitted job. No model call runs inside your database transaction. Individual operators such as NOUL are dispatched through `jev.submit`, rather than synchronous SQL predicates.

## Grant access

Install the extension and worker using [installation](INSTALLATION.md). Open an administrator session:

```bash
docker compose exec postgres psql -U sdd_admin -d sdd
```

Create a separate client login and assign its password interactively:

```sql
CREATE ROLE analyst LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
\password analyst
\q
```

Map that login to an application identity:

```bash
docker compose run --rm migrate sql-grant analyst --tenant demo --actor analyst --role reader
```

On an installation without Docker, use `jevsd-pg sql-grant` with administrator connection settings. Use `--role reviewer` only for clients allowed to approve or promote definitions. Do not share the runtime login `sdd_app` with SQL clients. The grant command rejects administrative logins and logins with runtime table access.

Connect as the client:

```bash
psql -h 127.0.0.1 -p 5432 -U analyst -d sdd
```

## First operator

In `psql`, which commits each statement by default:

```sql
SELECT jev.submit(
    'JEV.NOUL',
    '{"state":"The work is complete.","proposition":"The source reports completed work."}',
    limits => '{"max_judgments":1,"max_requests":1}',
    idempotency_key => 'completion-check-1'
) AS job_id \gset

SELECT jsonb_pretty(jev.result(:'job_id'));
```

Poll the last statement until `job_state` is `COMPLETED`, `FAILED` or `CANCELLED`. To poll interactively, append `\watch 1` after the result query. Press Ctrl+C to stop watching.

`COMPLETED` means the worker published a result, not that every semantic decision resolved. Inspect `output_state`, `operation_state`, `observations` and `manifest`. A completed job can contain `UNKNOWN`, a budget hold or a truncated result.

## Batch rows and concepts

The operator arguments match the [function reference](JEV_FUNCTION_REFERENCE.md). Batch independent subjects in one request to preserve shared context and parallel evaluation:

```sql
SELECT jev.submit(
    'JEV.TAG',
    '{
      "subjects": [
        {"message":"Please send the corrected invoice."},
        {"message":"问题仍未解决，请继续跟进。"}
      ],
      "concept_revs": ["Requests action", "Reports an unresolved problem"]
    }',
    limits => '{"max_judgments":4,"max_requests":4}'
) AS job_id \gset
```

For an application dataset, use `{"subjects":{"dataset_id":"your-dataset-id"},...}`. The worker resolves it under the mapped tenant and records source revisions. For your own SQL tables, build subjects with `jsonb_agg` from an authorized, explicitly bounded projection. Only send the fields needed for the task. Submitted JSON is a snapshot and does not automatically track later changes to external tables.

Requests are limited to 512 KiB and 100 queued or running jobs per tenant. Larger datasets should use registered dataset references or bounded batches. Operator budgets and server hard limits still apply.

## Natural language planning

```sql
SELECT jev.submit(
    'JEV.PLAN_SQL',
    '{"request":"For each supplier, show the total quantity delivered, largest total first."}',
    limits => '{"max_judgments":300,"max_requests":60}'
) AS job_id \gset

SELECT jev.result(:'job_id') -> 'value' -> 'plan';
```

Import and describe the dataset first. PLAN_SQL creates a proposal over the authenticated catalog and retains held interpretations. It does not execute generated SQL or commit changes. Inspect and execute proposals through the workspace or [query API](NATURAL_LANGUAGE.md), where catalog binding and review rules apply. The SQL interface does not grant access to internal tenant tables.

## Functions

| Function | Result |
| --- | --- |
| `jev.submit(operator text, arguments jsonb, limits jsonb, policy jsonb, idempotency_key text, approval_id text)` | Job UUID. Arguments, limits and policy default to `{}`; the last two parameters default to NULL. |
| `jev.result(job_id uuid)` | Operator result plus job state and timestamps. Available only to the submitting login while its tenant mapping remains valid. |
| `jev.cancel(job_id uuid)` | True if a queued request was cancelled. False if it had already started or finished. |

An idempotency key belongs to one login and exact request. Reusing it returns the existing job; changing the arguments under the same key is rejected. Keep the job UUID if the connection is interrupted after submission. Existing operator approvals can be passed through `approval_id`; they remain bound to the exact request, provider and budget.

| Job state | Result availability |
| --- | --- |
| `QUEUED`, `RUNNING` | `NOT_EVALUATED`, with a null value until a result is published. |
| `COMPLETED` | Read the operator's own output and operational states. False and zero are valid values. |
| `FAILED` | Failure metadata; no invented false result. |
| `CANCELLED` | `NOT_EVALUATED` / `CANCELLED`. |

The semantic states remain `VALUE`, `UNKNOWN` and `NOT_EVALUATED`. Queue state is separate from those states and from statuses such as `BLOCKED_BY_BUDGET`.

## Recovery and access changes

Workers claim distinct jobs with row locks and renew a 60-second lease. A worker that loses its lease cannot publish a result. The next queue scan marks expired work failed; it does not automatically replay an operator that may already have saved evidence or changed state. Inspect the returned `run_id` when available and the tenant's operator records before submitting a new request. Active model calls can outlive a worker interruption; this is not an exactly-once side-effect guarantee.

Stopping the worker stops new claims and allows current calls to finish. Compose allows 120 seconds before forced termination. `jev.cancel` only cancels queued jobs; use the existing operator-run cancellation API for active runs.

An administrator can revoke a login's mapping:

```sql
DELETE FROM jev.client_roles WHERE login = 'analyst';
```

New submissions and result reads are then denied, and queued jobs fail at dispatch. Work already claimed uses its captured identity. PostgreSQL login identity comes from `session_user`; connection pools must use the intended login, not a shared administrative login followed by `SET ROLE`.

Completed jobs are retained for result lookup and idempotency. Administrators can archive and delete terminal rows under their retention policy. Removing a job also removes its idempotency record; operator evidence has its own lifecycle.
