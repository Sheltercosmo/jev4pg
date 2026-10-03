# Native execution stage placement

The existing [hybrid planning DAG](ARCHITECTURE.md#hybrid-stage-placement) remains in place. This document records the native execution stages and why they wait or run concurrently.

| Stage | Required input | Independent work | Placement reason |
| --- | --- | --- | --- |
| Source selection | Caller privileges and explicit source SELECT | PostgreSQL's ordinary relational plan | Exact filters and projection reduce model context before dispatch. |
| Context identity | Selected row and typed question set | Other row identities | Identical contexts reuse an observation without removing duplicate result rows. |
| Durable evidence lookup | Authorized source context, active questions, evaluator and scope | Other context lookups | Reuse compatible committed observations before consuming query allowance. |
| Admission | Missing identities and remaining scan budget | Previously admitted provider requests | Charge only an accepted claim; exhausted work stays NOT_EVALUATED. |
| JEV evaluation | One context and its independent questions | Other admitted contexts | Questions share context in one request; bounded asynchronous I/O overlaps unrelated contexts. |
| Response validation | Complete provider response and declared question types | Other completed responses | Validate identity, probabilities and rubric before treating an observation as evidence. |
| Observation capture | Valid response, context identity, evaluator revision and questions | Other completed contexts | Keep raw evidence independent of thresholds; failed or skipped work cannot create an observation. |
| Durable publication | Current request token and validated observation | Other completed requests | Commit evidence independently of the source transaction; expose storage failure separately from the semantic outcome. |
| Decision resolution | Valid observation and thresholds | Other independent decisions | Uncertainty is a policy outcome, separate from transport failure. |
| Relational consumption | Typed decisions and projected source rows | PostgreSQL joins and arithmetic | Exact membership requires resolved decisions; missing work cannot become false. |

A source batch is a memory and admission boundary, not a semantic dependency. Independent source populations feed one bounded round-robin batch. Source-local ordinals and opaque routing identities preserve lineage while the executor overlaps provider requests. The iterator completes one batch before fetching another; overlap between batches remains a measured optimization opportunity. PostgreSQL execution stays on its backend thread; provider I/O is concurrent on a current-thread Rust scheduler.

Dependent concepts must be scheduled only after their actual inputs become available. They must not be placed together in a supposedly independent question batch. `scan_many` executes an independent frontier; integration with the existing planner's dependent stages is still planned.

Saved observations can enter directly at decision resolution after context and envelope validation. This path has no dependency on provider availability and can run with PostgreSQL's parallel-safe expression evaluation. Saving a population uses normal PostgreSQL transactions and table permissions; it does not imply automatic source refresh or shared provider admission.

The application compiler deduplicates repeated Boolean questions across aliases, then derives the required source population for each question. Questions about the same dataset with identical source populations share a scan and request context. Different branch populations remain separate, so unrelated question/row pairs cannot consume calls or make coverage incomplete. A NULL subject removes only its dependent question from dispatch. Proven conjunctive source filters run before evaluation; joins or ambiguous scopes retain the full selected population rather than guessing a smaller one.

The application submits all independent source populations to one native invocation. They share one executor, cache and query allowance; one large source cannot exhaust the first batch before other sources receive a turn. PostgreSQL cursor operations remain serial on their backend thread, and provider I/O overlaps across sources. The application reserves daily request allowance before dispatch in a separate committed transaction, then settles once from global usage. Coverage remains scoped to each source/question pair. Result assembly waits for one temporary decision relation and uses the same repeatable-read snapshot as source selection.

Before consuming incomplete evidence, the compiler proves a row-local partial-read fragment. Direct semantic projections preserve NULLs; Boolean predicates use SQL's three-valued logic without converting an unresolved value to a match. Population-dependent and NULL-consuming expressions wait for complete evidence. This is a consumer dependency, not a reason to serialize independent evaluation.

When configured, the native registry owns cross-query claims and provider admission. Lookup is batched before dispatch; independent missing contexts still use the shared asynchronous scheduler. Each claim commits its token, daily charge and provider slot before HTTP. A short local admission lock covers the claim and query counters, then releases before inference. Declined claims leave allowance available to other rows. Per-scan concurrency also respects the configured provider capacity. Cancellation leaves a durable attempt. Expired dispatches become uncertain and require reconciliation before another charge. A known semantic value survives publication failure with an unconfirmed receipt. The [registry decision](adr/0002-durable-native-evidence.md) explains source snapshots, evidence age and trust.
