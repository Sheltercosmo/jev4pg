# Native semantic execution

Development implementation of `jev_native`, a Rust PostgreSQL extension. It evaluates typed questions over an explicit SQL source and returns relational results without a Python worker. It is separate from the released 0.6.0 queue interface and is not yet connected to natural-language planning.

PostgreSQL filters, projects and joins source data. A cursor supplies bounded batches to Rust. Questions about one context share a request; independent contexts run concurrently. PostgreSQL receives the original projected row and its typed decisions.

## Build

The current target is PostgreSQL 17 on Linux, with its development headers, Rust 1.96 and libclang installed:

```sh
cargo install --locked cargo-pgrx --version 0.19.2
cargo pgrx init --pg17 /usr/lib/postgresql/17/bin/pg_config
cd native
cargo test -p jev-executor
cd pg
cargo pgrx install --sudo --pg-config /usr/lib/postgresql/17/bin/pg_config
```

The native CI workflow builds the extension and exercises it against a disposable PostgreSQL server. A deterministic HTTP fixture verifies transport and database behavior; it does not measure natural-language accuracy.

## Configure

An administrator supplies a JSON configuration file readable by the PostgreSQL operating-system user:

```json
{
  "endpoint": "http://127.0.0.1:9100/v1/systemone",
  "model": "your-pinned-model",
  "revision": "your-provider-revision",
  "api_key": ""
}
```

Set `JEV_NATIVE_CONFIG_FILE` to its absolute path in the PostgreSQL server environment before starting the server. Use HTTPS for a remote provider. The endpoint implements the [SystemOne contract](../docs/PROVIDERS.md#request-and-response-contract). SQL callers cannot replace the endpoint or key. Keep credentials out of SQL, query history and source control.

As an administrator:

```sql
CREATE EXTENSION jev_native;
GRANT USAGE ON SCHEMA jev_native TO analyst;
GRANT EXECUTE ON FUNCTION jev_native.scan(text,jsonb,jsonb) TO analyst;
```

The caller also needs access to its source tables and columns. Source reads run with the caller's PostgreSQL privileges and row security. The scan function is not executable by PUBLIC by default.

## Query

For a table `messages(id, body, received_at)`:

```sql
SELECT source->>'id' AS id,
       decisions->'action' AS decision
FROM jev_native.scan(
    $$SELECT id, body FROM messages
      WHERE received_at >= CURRENT_DATE - 7
      ORDER BY id$$,
    '{"action":{"type":"noul","instructions":"The message requests further action."}}',
    '{"max_rows":500,"max_judgments":500,"concurrency":4}'
);
```

Put exact filters and required columns inside the source SELECT. An outer `WHERE` or `LIMIT` does not promise to reduce semantic work. Source rows retain PostgreSQL JSON representations, including NULLs, Unicode and numeric values. Include a stable key when results need to identify individual rows.

The result has `ordinal`, `source`, `decisions` and `observation` columns. `ordinal` follows the source cursor's order; use an explicit source `ORDER BY` when order matters. Duplicate contexts can reuse observations, while duplicate result rows retain their multiplicity.

For exact Boolean membership:

```sql
SELECT source
FROM jev_native.scan(
    'SELECT id, body FROM messages',
    '{"action":{"type":"noul","instructions":"The message requests further action."}}'
)
WHERE jev_native.require_bool(decisions, 'action');
```

`require_bool` returns a resolved true or false and raises an error for unresolved or failed decisions. It prevents an incomplete population from silently producing an exact count. To review uncertainty, select the decisions directly.

## Save and reconsider evidence

An observation contains the model response, question definitions, evaluator revision, receipt time and a SHA-256 identity of the projected context. The provider URL and API key are not exported. Observations are separate from decisions: a valid uncertain answer has an observation; skipped or failed work has SQL NULL in `observation`.

Store results with ordinary PostgreSQL tables:

```sql
CREATE TABLE message_review AS
SELECT statement_timestamp() AS captured_at, result.*
FROM jev_native.scan(
    'SELECT id, body FROM messages ORDER BY id',
    '{"action":{"type":"noul","instructions":"The message requests further action."}}'
) AS result;
```

Apply a different decision threshold later, including from another connection:

```sql
SELECT source,
       CASE WHEN observation IS NOT NULL
            THEN jev_native.decide(source, observation, '{"accept":0.9,"reject":0.1}')
            ELSE decisions
       END AS decisions
FROM message_review;
```

`decide` performs no model call. It validates the stored response and checks that the supplied source matches its recorded context. Decimal spelling and JSON key order do not change identity; changed values, column names and missing fields do. It evaluates the recorded questions and model response, not a new question or a newer model revision.

This table is a saved population, not an automatically refreshed view of `messages`. Changes or deletions in the live source do not modify it. Refresh explicitly when the current population is required. Control its SELECT and write permissions as for other source data. The observation envelope detects context mismatch; it is not a cryptographic signature or a substitute for trusted table ownership. Callers can construct JSON, so applications must choose which evidence tables they trust.

## States and limits

| Output state | Meaning |
| --- | --- |
| `VALUE` | A resolved Boolean, choice or score. False and zero remain values. |
| `UNKNOWN` | A valid observation did not resolve under the decision policy. |
| `NOT_EVALUATED` | No usable observation is available. Read the operational status and reason. |

Operational status is separate: successful observations carry `SUCCEEDED`; exhausted admission carries `BLOCKED_BY_BUDGET`; transport or response-validation errors carry `FAILED`. No skipped or failed judgment becomes false.

Noul uses configurable `accept` and `reject` thresholds, defaulting to 0.8 and 0.2. Choice requires probability at least 0.55 for its selected option; option names have no reserved meaning. Score validates the declared rubric and numeric range. Raw answers accompany successful observations.

| Option | Default | Scope |
| --- | ---: | --- |
| `max_rows` | 10,000 | Source rows per scan; exceeding it aborts the statement. |
| `max_judgments` | 1,000 | Admitted questions across all batches. |
| `max_requests` | 1,000 | Admitted HTTP requests across all batches. |
| `max_input_bytes` | 8,000,000 | Serialized request bytes across all batches. |
| `batch_rows` | 32 | Source rows fetched at a time, at most 32. |
| `concurrency` | 4 | Independent in-flight requests per scan, at most 16. |
| `timeout_ms` | 45,000 | Timeout per HTTP request. |

One context is limited to 1 MB before Rust deserialization. Source and result batches each have an 8 MB serialized limit; reduce `batch_rows` for wide records. Responses are limited to 4 MB and the reuse cache to 8 MB of serialized content. These are data-size limits, not a hard cap on process memory. Oversized inputs cause errors rather than silent truncation. Duplicate source column names require explicit aliases. PostgreSQL statement cancellation is checked while waiting for responses; the HTTP client uses asynchronous DNS resolution.

## Remaining integration

Automatic reuse and admission currently belong to one scan invocation. Saved observations support explicit replay across sessions. Automatic evidence lookup, concurrent claims, budgets shared between sessions, live source revision tracking, maintained features and planner integration remain acceptance gates. This implementation does not replace all public JEV operators. The [implementation plan](../docs/IMPLEMENTATION_PLAN.md) tracks the larger change.

Model calls are external effects: transaction rollback cannot undo provider usage. Synchronous scans hold a PostgreSQL backend while inference runs. Restrict execution grants during development and use the released queue interface where its asynchronous behavior is required.
