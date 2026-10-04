# Native evidence registry

The development Rust executor can reuse committed observations and coordinate JEV requests across PostgreSQL connections. It keeps request ownership outside the source transaction, so a rolled-back query does not erase provider usage or successful evidence.

## Configure

Install the current native extension, then create a dedicated login as an administrator:

```sql
CREATE ROLE jev_registry LOGIN CONNECTION LIMIT 8;
GRANT USAGE ON SCHEMA jev_native TO jev_registry;
GRANT EXECUTE ON FUNCTION jev_native._registry_lookup(text,text[],integer) TO jev_registry;
GRANT EXECUTE ON FUNCTION jev_native._registry_claim(text,text,text,integer,integer,integer,integer,boolean) TO jev_registry;
GRANT EXECUTE ON FUNCTION jev_native._registry_finish(uuid,jsonb,text) TO jev_registry;
```

Configure local authentication for that login. In `psql`, `\password jev_registry` sets its password without embedding it in SQL history. Give this login no source-table access or administrative privileges.

Add a `registry` object to the server's `JEV_NATIVE_CONFIG_FILE`:

```json
{
  "endpoint": "http://127.0.0.1:9100/v1/systemone",
  "model": "your-pinned-model",
  "revision": "your-provider-revision",
  "registry": {
    "dsn": "host=127.0.0.1 port=5432 dbname=app user=jev_registry password=replace-locally",
    "max_active": 16,
    "max_daily": 10000,
    "max_age_seconds": 86400,
    "connect_timeout_ms": 2000
  }
}
```

Protect this file so only the PostgreSQL operating-system account and administrators can read it. Credentials never enter SQL options or result receipts. This implementation accepts local Unix sockets or explicit loopback IP addresses; remote registry connections are not supported yet.

Each active scan invocation uses at most one control connection. Reserve database capacity for the registry login in addition to application connections. The login's connection limit bounds this overhead. The control connection uses short committed operations, a two-second statement timeout and a 500 ms lock timeout. It holds no database transaction during provider I/O.

`max_active` bounds admitted requests for a provider endpoint across native scans. `max_daily` bounds dispatch admission for each authorized scope and endpoint per UTC day. Failed dispatches remain counted. Uncertain dispatches retain their active slot until settled or reconciled. These counters describe admitted work, not provider billing. Application query and daily allowances still apply as an additional bound.

## Query and reuse

```sql
SELECT source, decisions, receipt, usage
FROM jev_native.scan(
    'SELECT id, body FROM messages ORDER BY id',
    '{"action":{"type":"noul","instructions":"The message requests further action."}}',
    '{"evidence_scope":"message-review","max_requests":100}'
);
```

Run the same scan again to reuse compatible observations. Set `max_requests` to zero to allow only existing evidence. Changing `accept` or `reject` replays the raw response without a new model request.

Reuse requires the exact projected context, active typed question set and pinned provider/model revision. Changed values, column names or definitions miss the registry. Duplicate result rows remain present. Repacking independent questions into a different active batch currently creates a new request identity; reuse across different packing is still planned.

PostgreSQL supplies the source cluster, database and effective role identities. `evidence_scope` adds a namespace of at most 200 bytes. The application supplies its tenant identity here. A namespace does not replace PostgreSQL authorization or make a shared login safe for untrusted users. Its daily allowance is an operational budget within that namespace; callers allowed to choose new namespaces can open separate allowances.

The configured maximum age defaults to one day. A query can tighten it with `"evidence_max_age_seconds":60`; zero requests fresh evidence after a completed attempt. It never bypasses an active, failed or uncertain attempt. Include time and external dependencies in the declared context when they affect meaning. A stored observation is not a claim about a changing external world.

Source rows retain the query's PostgreSQL snapshot. Evidence lookup uses the newest compatible committed observation, with its original timestamp. An observation may have been committed after the source snapshot began. This is an on-demand semantic evaluation, not a historical query over an evidence snapshot.

## Read receipts

`receipt` is SQL NULL when the registry is not involved. Otherwise it includes `attempt_id` and `storage_state`.

| Storage state | Meaning |
| --- | --- |
| `STORED` | The new raw observation was committed. |
| `REUSED` | An existing observation supplied the result. |
| `UNCONFIRMED` | Publication was not confirmed; a known semantic result remains available. |
| `FENCED` | The request token was retired before publication. |
| `REJECTED` | Stored evidence failed expected-identity or response validation. |
| `DISPATCHING`, `UNCERTAIN`, `FAILED`, `CLOSED` | Inspect the corresponding attempt before deciding whether to retry. |

Semantic output remains `VALUE`, `UNKNOWN` or `NOT_EVALUATED`. Coordination can yield `BLOCKED_BY_CONCURRENCY`, `COORDINATION_SATURATED`, `COORDINATION_UNAVAILABLE`, `BLOCKED_BY_BUDGET`, `UNCERTAIN` or `BLOCKED_BY_REVIEW`. None means false. Exact aggregates still require complete decisions.

`usage.durable_reused_rows` counts result rows backed by reused stored observations. `usage.stored_observations` counts new committed observations. Usage fields remain cumulative across the scan; take their maximum rather than summing repeated counters. Application manifests retain receipts and distinguish native registry evidence from coverage-only records.

## Reconcile uncertain work

Administrators can inspect attempts with ordinary SQL:

```sql
SELECT id, scope, state, deadline, created_at, error
FROM jev_native.request_attempts
WHERE state IN ('DISPATCHING', 'UNCERTAIN', 'FAILED')
ORDER BY sequence DESC;
```

After establishing that the external request has ended, close it or explicitly permit a separately charged retry:

```sql
SELECT jev_native.reconcile_attempt(
    'replace-with-attempt-uuid',
    'RETRY_ALLOWED',
    'Provider confirmed the earlier request ended without a usable result'
);
```

Use `CLOSED` when no retry is wanted. Reconciliation retires the old token; its late response cannot overwrite a newer attempt. A valid late response can settle an uncertain attempt that has not been retired. Deadline expiry alone never grants permission to send again.

Raw observations are immutable. Registry writes use private functions granted only to the dedicated login. Retention, maintained-feature bindings, administrative UI and provider-supported idempotency remain integration work. The [architecture decision](adr/0002-durable-native-evidence.md) records these boundaries.
