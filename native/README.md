# Native semantic execution

Development implementation of `jev_native`, a Rust PostgreSQL extension. It evaluates typed questions over an explicit SQL source and returns relational results without a Python worker. The query service can also use it to execute `SEMANTIC` predicates in direct or generated SQL. It is separate from the released 0.6.0 queue interface.

PostgreSQL filters, projects and joins source data. A cursor supplies bounded batches to Rust. Questions about one context share a request; independent contexts run concurrently. PostgreSQL receives the original projected row and its typed decisions.

For dependent work, `jev_native.execute_plan` runs a typed stage DAG with PostgreSQL intermediates and one shared semantic executor. See [native plans](../docs/NATIVE_PLANS.md) and the [runnable SQL example](../examples/planning/native_plan.sql).

For reusable probability features, `jev_native.embed` batches a fixed basis of questions and returns an answer probability matrix and vector per row. [Native embeddings](../docs/NATIVE_EMBEDDINGS.md) covers projection, states, evidence reuse and local similarity queries.

## Build

For a complete container installation, use the [native Compose stack](../docs/NATIVE_DEPLOYMENT.md). The following commands install the extension on an existing server.

Build from the repository's `v0.7.0` tag for native preview 0.2.0, or `main` for ongoing development. The current target is PostgreSQL 17 on Linux, with its development headers, Rust 1.96 and libclang installed. The default Compose image does not include this extension. From the repository root:

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
GRANT EXECUTE ON FUNCTION jev_native.scan_many(jsonb,jsonb) TO analyst;
GRANT EXECUTE ON FUNCTION jev_native.execute_plan(jsonb,jsonb) TO analyst;
GRANT EXECUTE ON FUNCTION jev_native.embed(text,jsonb,jsonb) TO analyst;
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

The result has `ordinal`, `source`, `decisions`, `observation`, `usage`, `receipt` and `policy` columns. `ordinal` follows the source cursor's order; use an explicit source `ORDER BY` when order matters. Duplicate contexts can reuse observations, while duplicate result rows retain their multiplicity.

`usage` contains cumulative admitted requests, judgments, input bytes and reused rows. Take the maximum of each field across a completed scan, rather than adding the repeated counters. A request may fail after admission; these counters measure admission for dispatch, not provider billing.

A question can include `"subject_column":"body"` to require a non-NULL subject. A NULL subject produces `NOT_EVALUATED / SKIPPED` for that question. Other questions about the same row can still run together. This routing field is consumed by the executor and is not sent to the provider.

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

## Independent sources

Use `scan_many` to evaluate independent source populations through one scheduler:

```sql
SELECT source_id, ordinal, source, decisions
FROM jev_native.scan_many(
    '[
      {"id":"messages", "sql":"SELECT id,body FROM messages ORDER BY id",
       "questions":{"action":{"type":"noul","instructions":"The message requests further action."}}},
      {"id":"notes", "sql":"SELECT id,body FROM notes ORDER BY id",
       "questions":{"action":{"type":"noul","instructions":"The note requests further action."}}}
    ]',
    '{"max_rows":1000,"max_requests":500,"concurrency":4}'
);
```

Each declaration has a unique `id`, one source `sql` SELECT and its own `questions`. Up to 32 sources share the row, request, judgment, input-byte and concurrency limits. Source declarations together are limited to 2 MB. The scheduler reads bounded chunks and interleaves their rows; its position persists across batches. Empty sources emit no rows. Exhausted model allowance produces NOT_EVALUATED decisions for remaining rows, preserving coverage.

The result adds `source_id` to the single-source columns. `ordinal` starts at 1 within each source. Neither routing field is added to the provider context. Identical contexts and question sets can reuse one observation across sources without removing result rows; different definitions remain separate. All PostgreSQL reads stay on the calling backend with its privileges and snapshot, while independent provider requests overlap.

`usage` is cumulative for the entire invocation. Take its maximum once over the complete result; summing per-source maxima would count requests more than once. This interface accepts an independent frontier only. A question that needs another stage's output belongs in a subsequent stage.

## Save and reconsider evidence

For automatic reuse and request coordination across queries, configure the [native evidence registry](../docs/NATIVE_EVIDENCE.md). It stores observations independently of the source transaction and returns request receipts. The examples below remain useful for explicit evidence tables.

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
            THEN decisions || jev_native.decide(source, observation, '{"accept":0.9,"reject":0.1}')
            ELSE decisions
       END AS decisions
FROM message_review;
```

`decide` performs no model call. It validates the stored response and checks that the supplied source matches its recorded context. Decimal spelling and JSON key order do not change identity; changed values, column names and missing fields do. It evaluates the recorded questions and model response, not a new question or a newer model revision. If some questions were skipped, merge the replay with the saved decisions (`decisions || jev_native.decide(...)`) to retain their states.

This table is a saved population, not an automatically refreshed view of `messages`. Changes or deletions in the live source do not modify it. Refresh explicitly when the current population is required. Control its SELECT and write permissions as for other source data. The observation envelope detects context mismatch; it is not a cryptographic signature or a substitute for trusted table ownership. Callers can construct JSON, so applications must choose which evidence tables they trust.

## States and limits

| Output state | Meaning |
| --- | --- |
| `VALUE` | A resolved Boolean, choice or score. False and zero remain values. |
| `UNKNOWN` | A valid observation did not resolve under the decision policy. |
| `NOT_EVALUATED` | No usable observation is available. Read the operational status and reason. |

Operational status is separate: successful observations carry `SUCCEEDED`; exhausted admission carries `BLOCKED_BY_BUDGET`; transport or response-validation errors carry `FAILED`. No skipped or failed judgment becomes false.

Noul uses configurable `accept` and `reject` thresholds, defaulting to 0.8 and 0.2. Choice uses `choice_min` (default 0.55). Declare uncertainty category IDs with `unknown_options`; their selection yields UNKNOWN even at high confidence. Native option names have no reserved meaning by default: `none` can describe a real category. Supply the same explicit policy when moving a workflow between executors, including its uncertainty IDs.

Score validates the rubric, distribution and numeric range, then applies `score_confidence_min` (default 0). Missing confidence counts as zero. The returned `policy` records all thresholds, uncertainty IDs and its revision. `policy_revision` names the applied scan policy; `decide` accepts the returned policy object directly with its `revision` field. Revision names describe configuration and do not imply validated accuracy.

For a routing question with an explicit uncertainty option:

```sql
SELECT decisions, policy
FROM jev_native.scan(
    'SELECT id, body FROM messages',
    '{"route":{"type":"choice","instructions":"Which team should handle this message?",
       "criteria":{"support":"Product support","sales":"Purchase enquiry","unknown":"Insufficient evidence"}}}',
    '{"choice_min":0.8,"unknown_options":["unknown"],"policy_revision":"routing-v1"}'
);
```

Policy changes reuse compatible raw observations without a new provider call. Save the policy alongside the resulting decisions when later stages or reviewers need their meaning.

| Option | Default | Scope |
| --- | ---: | --- |
| `max_rows` | 10,000 | Total source rows per invocation, across all sources in `scan_many`; exceeding it aborts the statement. |
| `max_judgments` | 1,000 | Admitted questions across all batches. |
| `max_requests` | 1,000 | Admitted HTTP requests across all batches. |
| `max_input_bytes` | 8,000,000 | Serialized request bytes across all batches. |
| `batch_rows` | 32 | Source rows fetched at a time, at most 32. |
| `concurrency` | 4 | Independent in-flight requests per scan, at most 16. |
| `timeout_ms` | 45,000 | Timeout per HTTP request. |

One context is limited to 1 MB before Rust deserialization. Source and result batches each have an 8 MB serialized limit; reduce `batch_rows` for wide records. Responses are limited to 4 MB and the reuse cache to 8 MB of serialized content. These are data-size limits, not a hard cap on process memory. Oversized inputs cause errors rather than silent truncation. Duplicate source column names require explicit aliases. PostgreSQL statement cancellation is checked while waiting for responses; the HTTP client uses asynchronous DNS resolution.

## Use through the query service

After building and installing the extension on the PostgreSQL server, run the application migration with administrator credentials:

```sh
jev4pg migrate --native-interface
```

Set `SDD_SEMANTIC_ENGINE=native` for the application service. Its existing SQL and natural-language query routes then use Rust for ordinary `SEMANTIC(column, definition)` reads. The planner still produces inspectable SQL; the executor selects authorized source rows, batches questions in Rust, and returns temporary PostgreSQL relations for the remaining query. Full source populations do not pass through Python. Results identify `rust_postgresql` in the manifest and include the SQL execution steps.

This preview supports joins, subqueries, grouping and other relational calculations around Boolean semantic predicates. Structured filters are pushed into source selection only when their scope is proven safe. Questions with the same required source population share a scan and request context. Questions in different filtered branches retain their own populations, so unrelated NULL subjects neither consume calls nor make a result incomplete. Table column alias lists such as `items AS i(a,b,c)` are rejected until their ordinal lineage is supported; ordinary table aliases and SELECT column aliases work.

In the optimized base-column path, direct semantic references to the nullable side of an outer join are rejected: an unmatched joined row has no base-row observation. Grouping sets, ROLLUP and CUBE can likewise synthesize rows with different subjects. Evaluate the base source in a CTE before these operations when the intended operation is to join or aggregate already evaluated results. Semantic predicates on a guaranteed preserved join side and ordinary GROUP BY expressions remain supported.

For example, evaluate each message before calculating subtotals:

```sql
WITH evaluated AS (
    SELECT id, SEMANTIC(body, 'The message requests further action.') AS needs_action
    FROM messages
)
SELECT needs_action, COUNT(*) AS messages
FROM evaluated
GROUP BY ROLLUP(needs_action);
```

Incomplete evidence permits only a proven partial read: direct semantic projections and row predicates composed with AND, OR, NOT or Boolean equality, optionally across inner joins. Unresolved projections remain NULL and the manifest retains their UNKNOWN or NOT_EVALUATED coverage. Counts, subqueries, set operations, outer joins, ordering, limits and NULL-consuming expressions such as `COALESCE` require complete evidence. Otherwise execution reports the missing evidence instead of presenting an exact answer.

`SEMANTIC` can also judge a named column from a CTE or derived table. The service compiles these queries into [dependent native plans](../docs/NATIVE_PLANS.md#use-generated-or-handwritten-sql). PostgreSQL can first combine text, group records or calculate windows, then provide the resulting relation to JEV. A downstream SQL stage waits for its required decisions while independent semantic branches continue. Shared CTEs materialize once; the graph shares one budget and provider scheduler. This route requires complete decisions for its consumers and saves held targets as NOT_EVALUATED. Correlated or scalar subqueries and recursive CTEs remain unsupported in dependent semantic plans.

One native invocation evaluates the query's independent source populations, sharing question-context reuse, concurrency and query allowances. Database reservations share `SDD_DAILY_EVALUATIONS` with existing Python model calls. Completed scans settle their request count once and return unused allowance. Cancellation or a crash with an unknown dispatch count retains the reserved allowance; it does not assume the request was free. Direct SQL clients using `scan` or `scan_many` have their own explicit scan limits and are outside this application quota.

`SDD_NATIVE_CONCURRENCY` sets concurrent requests across the query's source populations (default 4). `SDD_NATIVE_MAX_ROWS` bounds their combined row count (default 100,000). `SDD_NATIVE_TIMEOUT_MS` bounds each application SQL statement (default 120,000 ms). The optional native registry adds provider admission across PostgreSQL sessions.

Maintained `SEMANTIC_FEATURE` reviews and semantic mutation previews still require `SDD_SEMANTIC_ENGINE=python`, the default. Native mode reports these unsupported paths explicitly. Ordinary relational mutations retain the existing preview and confirmation flow. With the registry configured, the query service retains durable receipts; otherwise it retains coverage summaries and explicit saved observation tables remain available.

## Current limitations

The optional registry provides automatic observation reuse, concurrent claims and provider admission across scans. Explicit plans support row conditions and local selected-branch merges; the query service uses them to lower [conditional SQL](../docs/NATIVE_PLANS.md#conditional-sql), including nested CASE. Reuse across differently packed question batches, live source revision tracking and maintained features remain acceptance gates. This implementation does not replace all public JEV operators. The [roadmap](../docs/IMPLEMENTATION_PLAN.md) tracks remaining release work.

Model calls are external effects: transaction rollback cannot undo provider usage. Synchronous scans hold a PostgreSQL backend while inference runs. Restrict execution grants during development and use the released queue interface where its asynchronous behavior is required.
