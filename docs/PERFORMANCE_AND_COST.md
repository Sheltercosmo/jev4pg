# Performance and cost

Version 0.5.0 adds configurable JEV endpoints and local Python adapters. Its checks cover provider compatibility, execution behavior and isolation of cached evidence. They do not establish natural-language accuracy, throughput or cost for a replacement model.

## What controls performance

Independent semantic work runs concurrently within the existing stage DAG. Questions with the same context can share a batch. Compatible observations are reused; a changed provider, model revision, source or question can require new work.

| Setting | Effect |
| --- | --- |
| `SDD_JEV_CONCURRENCY` | Concurrent JEV requests per tenant and database engine; default 4, maximum 16. |
| `SDD_PLANNING_WORKERS` | Workers for independent planning jobs; default 4, maximum 16. |
| Operator `limits.concurrency` | Workers admitted by an operator run, subject to provider limits. |
| Operator `limits.batch_size` | Questions sharing a context; default and maximum 32. |
| `SDD_HYBRID_CONCEPTS=off` | Avoid a preliminary LLM concept-generation call. |
| `SDD_HYBRID_REPAIR=on` | Allow one additional generation for an actionable defect. |

Local adapters receive the complete question batch and may be called concurrently. Their implementation determines whether the underlying model processes those questions together. Transport compatibility alone does not guarantee parallel inference.

## Cost controls

Operator budgets cap judgments, requests, estimated input tokens and estimated input cost. Inspect returned reservations, token usage and coverage. `JEV.EXPLAIN_PLAN` estimates missing work before dispatch.

Set `SDD_JEV_INPUT_USD_PER_MILLION` to the input-token rate for your provider. The default 0.042 is a configured accounting assumption, not a current price quotation. A zero rate excludes provider token charges but does not measure local compute or electricity.

Reported usage depends on the provider. Missing token counts cannot establish actual cost. Conservative admission estimates use UTF-8 byte bounds and are not exact token counts. Retries consume work reservations, and failed calls can still incur provider charges.

## Validation scope

The release tests exercise typed responses, all three primitives in one request, English and Simplified Chinese payloads, real loopback HTTP, local adapters, concurrency, provider failures and cache isolation. Synthetic responses make these checks deterministic; they are not model-quality scores.

Latency and answer accuracy must be measured for the configured provider on representative tasks. Keep SQL execution correctness, natural-language interpretation and result completeness separate. Held proposals should remain in evaluation, while failed and unexecuted cases retain their explicit states.
