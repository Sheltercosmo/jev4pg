# Semantic dictionaries and continuous input

Evaluate distinct semantic inputs, then map their decisions back to the original rows. This reduces inference when many records contain the same labels, categories or short descriptions. PostgreSQL still performs joins, counts and arithmetic.

Two interfaces are available:

* Existing native SQL can evaluate a distinct projected relation and join its results back. The [SQL example](../examples/operators/native_dictionary.sql) uses `jev_native.scan`.
* Native preview 0.3.0 adds `jev_native.scan_mapped`, using the Rust `record_map::Session` to combine independent questions across records and reuse individual judgments across cursor windows. Existing `scan` and application-planner behavior remain unchanged. Build from `main`; the application tag v0.7.0 includes native preview 0.2.0.

## Direct PostgreSQL usage

After [building and installing](../native/README.md#build) the current extension, run `CREATE EXTENSION jev_native` for a new installation. Existing 0.1.0 or 0.2.0 installations can use `ALTER EXTENSION jev_native UPDATE TO '0.3.0'`. Grant the new function explicitly:

```sql
GRANT EXECUTE ON FUNCTION jev_native.scan_mapped(text,jsonb,jsonb,jsonb) TO analyst;
```

Query existing tables without copying them into an application service:

```sql
SELECT item.ordinal, item.source, item.decisions
FROM jev_native.scan_mapped(
    'SELECT id,status FROM work_items ORDER BY id',
    '{"follow_up":{"type":"noul","instructions":"Does the status indicate that someone must follow up?","subject_column":"status"}}',
    '{"follow_up":["status"]}',
    '{"max_rows":10000,"max_judgments":1000,"concurrency":4}'
) AS batch
CROSS JOIN LATERAL jsonb_to_recordset(batch.rows)
    AS item(ordinal bigint, source jsonb, decisions jsonb);
```

The function returns `window_ordinal`, `rows`, `evidence`, `usage` and `policy`. Each row entry retains its global source ordinal, complete selected source and typed decisions. `evidence.observations` stores complete provider responses once per window; `evidence.bindings` maps questions to observations using zero-based positions within that window. Bindings are not standalone observations for `jev_native.decide`. `evidence.receipts` retains available durable attempt receipts, including failures without an observation.

`usage.executor` is cumulative across the invocation; take its final value or field maxima, not a sum across windows. `usage.window` reports new and reused judgments for that window. The complete serialized SQL result window is capped at 8 MB. An outer LIMIT may stop between windows, but does not promise row-at-a-time inference.

The options `map_window_rows` (default 512, range 1–512), `map_cache_bytes` (default 8,000,000) and `map_cache_age_seconds` (default 300) configure windowing and retention. All existing native request, judgment, input-byte, row, concurrency and registry options still apply. Source reads retain invoker privileges and row-security policies. In-process caches belong to one invocation; the optional durable registry controls reuse across invocations under its own scope and age policy.

## How work is reduced

For question `q`, declare the context projection `Pq(row)` containing every field it needs. The executor forms the distinct set `Dq = DISTINCT(Pq(rows))`. JEV evaluates each missing `(question, context, evaluator)` once. Results are mapped to all original rows, including duplicates.

With independent questions, work is proportional to `sum(|Dq|)`, rather than `rows × questions` or a Cartesian product of candidate fields. For example, two questions depending on columns with three and two distinct values need five judgments, even over millions of combinations. SQL still scans or indexes those rows; inference reduction does not eliminate database work.

Projection is an explicit contract. A question about occupation may depend only on `occupation`; a question about account eligibility may also need country, date and account state. Without a declaration, the full record is used. The executor does not guess which fields are irrelevant.

## Rust usage

The following uses the `jev-executor` crate in `native/core` with an existing configured `Executor`:

```rust
use jev_executor::{Executor, Questions};
use jev_executor::record_map::{Projections, Session};
use serde_json::json;

fn classify(executor: &mut Executor) -> Result<(), &'static str> {
    let questions: Questions = serde_json::from_value(json!({
        "follow_up": {
            "type": "noul",
            "instructions": "Does the status indicate that someone must follow up?",
            "subject_column": "status"
        }
    })).unwrap();
    let columns: Projections = serde_json::from_value(json!({
        "follow_up": ["status"]
    })).unwrap();
    let mut session = Session::default();
    let first = session.evaluate(executor, &[
        json!({"id": 1, "status": "Waiting for a response"}),
        json!({"id": 2, "status": "Waiting for a response"})
    ], &questions, &columns, || {})?;
    assert_eq!(first.decisions.len(), 2);
    assert_eq!(first.unique_judgments, 1);

    let next = session.evaluate(executor, &[
        json!({"id": 3, "status": "Waiting for a response"}),
        json!({"id": 4, "status": "Completed"})
    ], &questions, &columns, || {})?;
    // Reuse requires valid, unexpired evidence from the first call.
    println!("new: {}, reused: {}", next.unique_judgments, next.reused_judgments);
    Ok(())
}
```

Keep the session between input windows. Each call accepts at most 512 records and 8 MB of serialized source. Questions, prepared work, decisions and returned evidence have additional bounds. A request contains at most 32 questions and a conservative context byte limit. Independent requests use the executor's existing concurrency, admission and cancellation checks. No LLM is called.

Each question carries its own structured record. This avoids resolving row positions inside a shared array. Dependent questions belong in later DAG stages; only questions with available inputs can share a batch. Large shared contexts may be cheaper through the existing one-context/multiple-question executor, so this layout is opt-in.

## Cache and evidence

The default cache allows 8 MB of accounted serialized evidence and identity metadata, with a five-minute TTL. This is an accounting bound, not an exact process-memory limit. `Session::new(bytes, Duration)` accepts up to 8 MB and one day; zero capacity or age disables retention. Least recently used request batches are evicted as needed. A batch is the eviction unit because its questions share one authentic provider observation.

Identity includes the projected values, question instructions, type, candidates or rubric, endpoint identity and pinned model revision. JSON key order and equivalent decimal spellings do not change identity; strings, booleans and different exact integers remain distinct. Adding an independent question does not invalidate existing judgments. Changing thresholds reinterprets saved probabilities without another request. Each retained response is validated once per input window under the current policy, then its decisions are mapped to all matching rows.

`decisions` preserves input order and multiplicity. `bindings` connects each row/question to a provider question in `observations`. These are the actual complete request observations, not fabricated per-row responses. `unique_judgments` counts missing identities, including work subsequently blocked by admission; `reused_judgments` counts rows served from retained evidence. Provider usage remains on the executor.

Use one session per authorization and evidence scope. Observations can contain several records, so never share them across principals without a reviewed access policy. Call `clear()` when the scope or external domain knowledge changes. This cache is in-process; it is not a cross-connection or restart-persistent cache. If the executor also uses a durable registry, configure that registry's age and scope separately: its batch-level reuse follows its own freshness policy.

Valid uncertain observations remain `UNKNOWN`. Missing required subjects remain `NOT_EVALUATED / SKIPPED`; failed or budget-blocked work is not cached as false. Use `Decision::require_bool()` for exact membership and aggregate only after every required decision resolves.

## Candidate maps and aggregation

For extraction or matching, code can enumerate candidate spans or keys, JEV can select among those candidates, and code can copy the selected value or perform arithmetic. Include an explicit no-match option. Candidate generation must retain the answer; approximate retrieval alone does not establish complete coverage.

Reusable probability embeddings offer another local step: calculate the query basis once, then compare compatible stored matrices. This ranks candidates without repeating inference for every pair. It does not make approximate neighbors interchangeable for exact filtering.

The implementation follows TypeSafe's guidance on [structured questions](https://docs.typesafe.ai/primitives/advanced), [parallel questions](https://docs.typesafe.ai/patterns/fan-out) and [candidate selection](https://docs.typesafe.ai/cookbooks/pre_parsed_value_extraction_cookbook). See the [benchmark protocol](../benchmarks/semantic_filtering/README.md) for measurement scope and reproducibility.
