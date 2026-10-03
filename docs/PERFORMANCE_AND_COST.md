# Performance and cost

Version 0.5.0 matched all 11 SQL references in the public simple-query tutorial using JEV, GPT-5.6 Terra or their hybrid. JEV achieved the same result agreement with no LLM generation. Terra had the lowest median elapsed time on this small workload.

## Simple-query results

Measured on 3 October 2026 against the published [0.5.0 source](https://github.com/Sheltercosmo/jevsd-pg/tree/ecea7d8a49ba05541776f1d6fc674b4395e15dae). The [data, runner and records](../examples/nl2sql/README.md) are included.

| Method | Basic objectives | Paired variants | Median time | Proposals held for review |
| --- | ---: | ---: | ---: | ---: |
| JEV 1.13.0 | 8/8 | 3/3 | 5.51 s | 11/11 |
| GPT-5.6 Terra, low | 8/8 | 3/3 | 4.94 s | No review stage |
| JEV + GPT-5.6 Terra, low | 8/8 | 3/3 | 7.21 s | 11/11 |

These are matching SQL proposals, including holds. The evaluator executed each proposal in isolation; the application still requires approval for held work. All 33 outputs were complete and matched the references. The LLM baseline generated one query per request without repair or execution feedback. Hybrid used one LLM generation per request; no extra generation was needed.

Two six-row synthetic datasets cover counts, sums, averages, grouping, numeric filtering, ordering and distinct counts. Three paired checks add a paraphrase, Simplified Chinese and a renamed schema with equivalent descriptions. NULLs, duplicate group values and tied scores are present. This is a tutorial check, not a held-out benchmark or evidence of parity on complex SQL.

Reference SQL was verified against explicit expected rows before inference. Scoring ignores aliases and global output-column permutation, preserves duplicate rows and requested order, and rounds numbers to six decimal places. No case has an empty reference result. Incomplete outputs cannot pass.

Timing includes planning or generation and guarded SQLite execution, excluding data setup and queue wait. Each method and case starts with a fresh database and application cache. At most two jobs overlap. Terra runs through a fresh Codex CLI process, so its timing includes startup; it is not a direct API latency measurement. One measured run does not establish a stable speed ranking.

## Recorded usage

Totals across 11 requests per method:

| Method | JEV requests / judgments | JEV input / output tokens | LLM calls | LLM input / output tokens |
| --- | ---: | ---: | ---: | ---: |
| JEV | 215 / 731 | 244,257 / 29,645 | 0 | 0 / 0 |
| GPT-5.6 Terra | 0 / 0 | 0 / 0 | 11 | 104,485 / 335 |
| Hybrid | 40 / 180 | 35,498 / 4,740 | 11 | 118,408 / 1,013 |

Terra reported 62,720 cached input tokens for the baseline and 63,488 for hybrid. CLI usage includes its instruction overhead. On these tiny catalogs, hybrid used more LLM tokens and more time than the baseline; its benefit here is the additional semantic review.

Actual billed cost was not measured. At the [published Terra API rates](https://developers.openai.com/api/docs/models/gpt-5.6-terra) checked on 3 October 2026, the recorded LLM usage corresponds to approximately $0.1001 for the baseline and $0.1347 for hybrid, before JEV charges. These are API-equivalent estimates, not Codex subscription charges. The calculation uses $2 per million uncached input tokens, $0.20 cached input and $12 output; reported cache-write tokens were zero.

For a JEV contract billed only on input at the configured $0.042 per million tokens, recorded input would cost $0.01026 for JEV-only or $0.00149 for hybrid. Apply your provider's actual input and output rates; this assumption is not a current TypeSafe price quotation.

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

The 504 release tests separately cover typed responses, all three primitives in one request, English and Simplified Chinese payloads, real loopback HTTP, local adapters, concurrency, provider failures and cache isolation. Their synthetic responses make those checks deterministic. The tutorial measurements above use live models. Neither set establishes accuracy for a replacement provider.

Latency and answer accuracy must be measured for the configured provider on representative tasks. Keep SQL execution correctness, natural-language interpretation and result completeness separate. Held proposals should remain in evaluation, while failed and unexecuted cases retain their explicit states.
