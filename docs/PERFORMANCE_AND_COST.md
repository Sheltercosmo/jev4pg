# Performance and cost

Performance records cover two distinct workloads: [operator execution](benchmarks/NATIVE_OPTIMIZATION.md) compares current development with v0.8.0, and [semantic filtering](benchmarks/SEMANTIC_FILTERING.md) compares native 0.3.0 with the pg-jev 0.2.1 request path. The former measures local computation and reuse; the latter measures language accuracy, executor latency, provider calls and tokens.

## Optimized operator execution

In paired local measurements, reading 600 cached operator judgments fell from 162.1 to 8.0 ms (**20.4× faster**), packing 1,024 questions from 22.5 to 7.4 ms (**3×**), and projecting 10,000 native probability matrices from 61.1 to 16.7 ms (**3.7×**). Bulk lookup reduced evidence SELECTs from 600 to 3; exact-size packing retained the same 32 request bundles.

These changes serve the shared Python operator runtime and native 0.4.0 executor. Native cache retention removed 32 redundant provider requests under mixed recurring and new inputs. The deterministic provider makes these computation and request-count measurements; it does not measure billed savings or language accuracy. Warm native scan latency was 9% higher in this run. See [all results and reproduction](benchmarks/NATIVE_OPTIMIZATION.md) and [optimizations by function](NATIVE_OPTIMIZATION.md).

## Semantic filtering comparison

On the frozen 400-review holdout, mapped execution reached 98.0% accuracy versus 92.0%, at median latency of 469 versus 732 ms. Three independent predicates required 38 rather than 60 requests. With the same declared-column projections, input tokens fell from 1,495 to 954. The complete-record review case used 9.7% more input tokens, and the single-predicate case was slower. See [all workloads, limits and reproduction](benchmarks/SEMANTIC_FILTERING.md).

## BIRD Challenging comparison

The [archived 100-question BIRD evaluation](benchmarks/BIRD_CHALLENGING_100.md) compares JEV, GPT-5.6 Terra and hybrid planning across 11 databases. It records SQL answer matches, latency, calls and estimated token cost, including held proposals. Hybrid used 20.8% fewer LLM input tokens; the LLM baseline matched more answers. The measurements describe the frozen Python planner from 23 September 2026, not v0.7.0 or the native preview.

## Where calls are spent

| Operation | Provider work |
| --- | --- |
| Exact SQL filtering, joins and arithmetic | None. PostgreSQL performs the calculation. |
| Native scan or embedding | Missing questions sharing one context are batched in a request. Independent contexts run concurrently within configured limits. |
| Native `scan_mapped` | Missing exact question/context pairs are packed together. Per-question dictionaries and a bounded invocation cache reduce repeated inference across windows. |
| Compatible observation reuse | No new inference for the reused observation. Persistent reuse requires the evidence registry. |
| `decide`, `answer_matrix`, `embedding_distance`, `embedding_distances` | None. These apply policy, project distributions or compare stored matrices locally. Batch distance is available in native 0.4.0 development. |
| Hybrid planning | One SQL generation by default; a supported repair can add one. JEV context selection and review have separate usage. |

Sharing context reduces repeated input; concurrency overlaps independent requests. Neither guarantees a fixed speedup. Distinct questions still count as separate judgments. SQL rollback cannot undo provider usage.

## Read usage correctly

Native results include `usage`, `receipt` and `policy`. Usage records cumulative admitted requests, judgments, input bytes and reused rows for the invocation. Take the maximum counters over the complete result; do not sum repeated row counters. Admission is not provider billing: a request can fail after admission.

`scan_mapped` returns cumulative counters in `usage.executor` and per-window counters in `usage.window`. Shared provider observations, row bindings and available registry receipts are in `evidence`. [Mapped result contract](NATIVE_RECORD_MAP.md#direct-postgresql-usage).

For API operators and hybrid queries, inspect returned usage and plan details. Separate LLM generation, JEV selection and review, and data execution. Record missing or failed work even when a proposal remains available for review.

Dollar cost depends on the configured service or local hardware. `SDD_JEV_INPUT_USD_PER_MILLION` controls the application's input-token estimate; its default `0.042` is an accounting assumption, not a provider quote. Include output charges and local compute separately. Native byte counters are not token counts or a billing estimate.

## Reduce unnecessary work

Put exact filters and only required fields inside native source SQL. An outer LIMIT does not promise to reduce provider work. Use a compact question basis for embeddings, preserve its identity, embed each search input once and compare stored vectors locally.

Use `scan_many` or a shared stage plan for independent populations. Configure the [evidence registry](NATIVE_EVIDENCE.md) for reuse across connections. Changing source content, questions or evaluator revision can require new observations.

Use [`scan_mapped`](NATIVE_RECORD_MAP.md) for many small independent judgments or repeated values. Declare every required context field per question; the executor can then evaluate distinct inputs and restore row multiplicity locally. Cache capacity and age are bounded, and the in-process session belongs to one SQL invocation.

Native `concurrency` defaults to 4 and is limited to 16 per invocation. `max_requests`, `max_judgments`, `max_rows` and `max_input_bytes` bound different resources. The registry adds shared admission across sessions. Synchronous native calls hold a PostgreSQL backend during inference; size connection pools accordingly. Attached view sources need an additional guard connection.

See [native limits](../native/README.md#states-and-limits), [hybrid configuration](HYBRID_QUERY.md) and [operator budgets](JEV_OPERATORS.md).

## Reproduce and compare

The [NL2SQL tutorial](../examples/nl2sql/README.md) supplies synthetic data, references and a runner for JEV, LLM and hybrid planning. Validate the references without model calls:

```bash
python examples/nl2sql/compare.py --validate-only
```

For native execution, use the [SQL examples](../examples/operators/README.md#native-sql) and PostgreSQL integration workflow as correctness starting points. Its deterministic provider is a test fixture, not a model benchmark.

A useful comparison records the source commit, PostgreSQL and provider versions, schema, row count, question basis, budgets and cache state. Run cold and reused-evidence cases separately. Report result matches, incomplete cases, median and tail latency, provider requests, tokens, peak memory and connection count. Include held proposals in result scoring and preserve failed or unexecuted cases. Language accuracy and embedding retrieval quality need their own untouched test cases.
