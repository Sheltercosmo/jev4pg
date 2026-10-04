# Durable application queries

Submit SQL once, keep its job ID, and retrieve the result after an HTTP disconnect or API restart. The development application provides durable jobs for reads and mutation previews. PostgreSQL stores the request, ownership lease and outcome; a separate worker executes the query through the existing SQL and semantic runtime.

Natural-language planning still uses `/ask`. Submit its reviewed SQL when you need background execution. Jobs preserve the existing result, source and inference limits; they are not a bulk export or unrestricted population-scan interface.

## Start a worker

Upgrade the application catalog to version 4 with `sdd migrate` using the installation owner. Use the restricted runtime credentials for the API and workers. Stop older application processes before upgrading and keep a backup. The migration adds the job table and tenant policies while retaining existing catalog objects and data.

Run a worker in a separate service process:

```sh
sdd query-worker --tenant analytics --concurrency 2 \
  --heartbeat-file /tmp/jev-query-worker.heartbeat
```

Repeat `--tenant` for additional tenant scopes. Multiple workers can serve the same scope; each claim uses PostgreSQL row locking with `SKIP LOCKED`. A worker visits its configured scopes in turn, so a busy tenant cannot monopolize its submission loop. Concurrency is 1–8 per process, default 2. Each query retains its own independent JEV concurrency and admission budgets.

Use the same provider configuration as the API. `--once` performs one claim attempt per tenant per worker slot and then exits. A supervisor should restart a failed worker process; it must not resubmit its claimed jobs. For a configured heartbeat file, check liveness with:

```sh
sdd ready --worker-heartbeat /tmp/jev-query-worker.heartbeat
```

Queue control uses a separate connection pool from source execution. This lets a worker observe cancellation while its source connection is occupied. Budget PostgreSQL connections for both pools and the existing source guards. Production workers require PostgreSQL and the libpq 17 cancellation support described in [query controls](QUERY_CONTROL.md).

## Submit and retrieve

Send a reader or reviewer bearer token. Use one idempotency key for one intended execution:

```sh
curl -X POST http://127.0.0.1:8000/data/query-jobs \
  -H "Authorization: Bearer $SDD_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT category, COUNT(*) AS total FROM events GROUP BY category",
       "idempotency_key":"daily-summary-2026-10-04",
       "timeout_seconds":30}'
```

The response is HTTP 202 with `id`, `history_id`, `job_state`, `output_state` and `operation_state`. `result` is null until a successful outcome is published. Poll `GET /data/query-jobs/{id}` with the same identity. On success, `result` contains the ordinary SQL response: rows, manifest, run ID and SQL details. Polling never starts another execution.

Repeating the same key and normalized request returns the same job, including after cancellation or failure. Reusing it for different options or SQL returns HTTP 409. Keys are scoped to tenant and actor. To deliberately rerun work, submit a new key; compatible semantic evidence may still be reused.

| Route | Purpose |
| --- | --- |
| `POST /data/query-jobs` | Submit SQL with `idempotency_key` and the ordinary SQL execution options. |
| `GET /data/query-jobs/{id}` | Retrieve this actor's status and published result. |
| `GET /data/query-jobs?limit=20` | List recent job summaries. Send `next_cursor` as `before` for the next page. |
| `POST /data/query-jobs/{id}/cancel` | Cancel queued work or request cancellation of running work. |

Each job also appears in query history. `parent_history_id` connects a revised request to earlier work. A reviewer can submit a mutation preview; applying it still requires the normal separate commit route and an unexpired preview token. Jobs cannot submit write commits.

## States and recovery

| Job state | Meaning |
| --- | --- |
| `QUEUED` | Persisted and waiting for a worker. No query result yet. |
| `RUNNING` | A worker owns a live lease. |
| `CANCELLING` | Cancellation was requested; execution and admitted provider work are settling. |
| `SUCCEEDED` | The SQL service returned an outcome and its owner published it. Inspect the output and operation states below. |
| `CANCELLED` | Cancellation completed. No job result is published. |
| `TIMED_OUT` | The execution deadline stopped the query. |
| `FAILED` | Validation, execution or worker ownership failed. `error` contains a category, not raw driver messages or source values. |
| `REDACTED` | Managed source deletion removed this job's request and result and fenced further publication. |

`VALUE` means a resolved SQL outcome. `UNKNOWN` with `operation_state: PARTIAL` means unresolved semantic work remains; an empty partial result is not proof that no rows match. `NOT_EVALUATED` means no completed job output is available. Truncated results report `TRUNCATED`; mutation previews report `AWAITING_REVIEW`. These states remain separate from job scheduling status.

Cancellation of a queued job prevents claiming. Running cancellation targets only that execution's connections. Already admitted JEV requests can finish, incur usage and contribute reusable evidence; the worker renews its lease while they settle. A cancellation accepted before publication wins over a concurrently computed result. A terminal job is not changed by a later cancel request.

Claims last 30 seconds by default and are renewed while work is active. If a worker disappears or loses ownership, a status read or later claim marks the expired job `FAILED` with `LEASE_EXPIRED`. Its result cannot be published by the stale worker, and it is not automatically retried. Some provider work or an internal SQL run may already exist. Inspect the failure before choosing a new submission key. This is not an exactly-once guarantee for remote inference.

The query deadline starts at execution, excluding queue wait. The job binds the catalog identities and definitions seen at submission, but reads source rows at execution time. A changed catalog requires a new request. PostgreSQL permissions, source checks, query limits and provider budgets are still enforced during execution.

Results retain the SQL endpoint's 1,000-row cap. A job outcome above 4 MiB fails with `RESULT_TOO_LARGE`; reduce the projection or use PostgreSQL export tooling. Internal run storage and already incurred inference are not undone by this publication limit. Jobs and their idempotency keys are retained; automatic retention and resumable bulk execution remain planned. Revoking an API token does not revoke a previously accepted job: cancel it through the submitting identity or an administrator's controlled workflow.

## Validate

With `SDD_TEST_ADMIN_URL` set to an isolated PostgreSQL server:

```sh
python -m pytest tests/test_query_jobs.py tests/test_query_jobs_postgres.py tests/test_query_jobs_final_postgres.py -q
```

The tests cover simultaneous idempotent submissions, exclusive claims, process-level cancellation, worker death, publication fences, tenant and actor access, retained JEV evidence and migration from catalog version 3. Further cases exercise the worker CLI with Chinese catalogs, source rebinding, deadlines and managed deletion. Fixtures use generated data and deterministic providers. They measure execution contracts, not model accuracy or deployment capacity.

The queue follows PostgreSQL's documented [row locking and `SKIP LOCKED` behavior](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE). Locks cover claim and publication transactions; model calls run outside them. See [stage placement](JEV_PLANNING_STAGES.md) for the parallel execution boundaries.
