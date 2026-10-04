# Query cancellation and deadlines

The development application supports cancellation handles for SQL reads and mutation previews, plus an optional execution deadline on `POST /data/sql`. This is an in-process execution control. [Durable query jobs](QUERY_JOBS.md) use it for cancellation across service processes. The existing `jev.cancel` operator SQL queue function still cancels queued jobs only.

## API execution deadline

Send an authenticated SQL request with `timeout_seconds`:

```json
{
  "sql": "SELECT category, SUM(amount) AS total FROM events GROUP BY category",
  "timeout_seconds": 30,
  "max_evaluations": 100
}
```

The deadline starts when the SQL service begins execution, after authentication and the initial permission check. It covers source reads and semantic execution. The API accepts 0.1 to 3,600 seconds. Omit it to keep existing behavior. PostgreSQL statement, lock and provider limits still apply; this option does not increase those limits.

A deadline produces HTTP 408 with `output_state: NOT_EVALUATED`, `operation_state: TIMED_OUT`, `result: null` and a `history_id`. History retains the input with a timed-out status, so users can edit and rerun it. A timeout is not an empty successful result or a false semantic judgment.

## Cancel from an application

Pass a fresh `QueryControl` to `SQLService.execute`. Keep the handle with the application request and call `cancel()` from another thread when that user asks to stop:

```python
from concurrent.futures import ThreadPoolExecutor

from sdd.generic.sql import SQLService
from sdd.query_control import QueryControl, QueryInterrupted

service = SQLService(db, decisions)
control = QueryControl(timeout_seconds=30)

with ThreadPoolExecutor(max_workers=1) as pool:
    pending = pool.submit(
        service.execute,
        "team",
        "SELECT category, COUNT(*) AS total FROM events GROUP BY category",
        control=control,
    )
    # Connect the authenticated request's cancel action to control.cancel().
    try:
        result = pending.result()
    except QueryInterrupted as error:
        print(error.output_state, error.operation_state)
```

`db` is the configured `Database`; `decisions` is a JEV decision service, or `None` for ordinary SQL. `Future.cancel()` alone cannot interrupt a running query.

| Interface | Contract |
| --- | --- |
| `QueryControl(timeout_seconds=None)` | One execution per handle. A finite positive deadline up to 3,600 seconds is optional. |
| `control.cancel()` | True when this call first records cancellation, including before execution starts. False if already stopped or finished. This acknowledges the request, not completed cleanup. |
| `SQLService.execute(..., control=control)` | Executes under that handle. Source connections are registered only while held by this query. |
| `QueryInterrupted` | `output_state` is `NOT_EVALUATED`; `operation_state` is `CANCELLED` or `TIMED_OUT`. Messages contain no SQL or source values. |
| `control.cancel_errors` | Exception class names from failed cancellation requests, without credentials or driver exception text. |

Read and semantic result limits are unchanged. Mutation previews still require a separate commit, and controls cannot be used for write commits. Do not automatically retry a write whose outcome is uncertain.

## Execution contract

Controlled PostgreSQL reads use Psycopg's [connection cancellation](https://www.psycopg.org/psycopg3/docs/api/connections.html#psycopg.Connection.cancel_safe), requiring libpq 17 or newer. The handle targets held connections rather than stored backend PIDs. Registration and cancellation share a lock, so a late request cannot cancel the next user of a returned pooled connection. A failed cancellation transport causes the connection to be discarded when execution unwinds. SQLite uses its connection interrupt mechanism for local development.

Independent JEV requests retain their concurrency. Cancellation stops scheduling batches after the signal is observed. Already admitted Python provider calls settle and their valid evidence and usage records remain; the cancelled query does not return a complete result. A provider can still charge for dispatched work. Later evidence reuse follows the normal source and revision checks.

The native extension checks PostgreSQL interrupts in its execution loop. The control targets its source connection as it does ordinary SQL. These Python integration tests do not qualify cancellation across all native provider and deployment combinations.

Cancellation is cooperative. Connection acquisition, source validation outside a controlled transaction, Python computation and already dispatched provider requests can delay cleanup. Wait for execution to settle after requesting cancellation. Evidence or an internally saved run/preview can remain if cancellation races with completion; the handle will not return that execution as successful after accepting the signal.

## Validation

Set `SDD_TEST_ADMIN_URL` to a disposable PostgreSQL server and run:

```sh
python -m pytest tests/test_query_control.py tests/test_query_control_postgres.py tests/test_query_control_final_postgres.py -q
```

Tests cover concurrent PostgreSQL queries, connection reuse, source lock waits, Chinese identifiers, deadline responses, retained history, failed cancellation transport and parallel provider work. The deterministic provider fixture verifies scheduling and evidence retention, not model quality or billing.
