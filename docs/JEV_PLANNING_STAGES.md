# Native execution stage placement

The existing [hybrid planning DAG](ARCHITECTURE.md#hybrid-stage-placement) remains in place. This document records the native execution stages and why they wait or run concurrently.

| Stage | Required input | Independent work | Placement reason |
| --- | --- | --- | --- |
| Source selection | Caller privileges and explicit source SELECT | PostgreSQL's ordinary relational plan | Exact filters and projection reduce model context before dispatch. |
| Context identity | Selected row and typed question set | Other row identities | Identical contexts reuse an observation without removing duplicate result rows. |
| Admission | Missing identities and remaining scan budget | Other admitted contexts | Reserve before dispatch; exhausted work stays NOT_EVALUATED. |
| JEV evaluation | One context and its independent questions | Other admitted contexts | Questions share context in one request; bounded asynchronous I/O overlaps unrelated contexts. |
| Response validation | Complete provider response and declared question types | Other completed responses | Validate identity, probabilities and rubric before treating an observation as evidence. |
| Observation capture | Valid response, context identity, evaluator revision and questions | Other completed contexts | Keep raw evidence independent of thresholds; failed or skipped work cannot create an observation. |
| Decision resolution | Valid observation and thresholds | Other independent decisions | Uncertainty is a policy outcome, separate from transport failure. |
| Relational consumption | Typed decisions and projected source rows | PostgreSQL joins and arithmetic | Exact membership requires resolved decisions; missing work cannot become false. |

A source batch is a memory and admission boundary, not a semantic dependency. The initial native iterator completes one batch before fetching another; overlap between batches remains a measured optimization opportunity. PostgreSQL execution stays on its backend thread; provider I/O is concurrent on a current-thread Rust scheduler.

Dependent concepts must be scheduled only after their actual inputs become available. They must not be placed together in a supposedly independent question batch. Integration with the existing shared stage DAG is still planned; the current native scan accepts independent questions only.

Saved observations can enter directly at decision resolution after context and envelope validation. This path has no dependency on provider availability and can run with PostgreSQL's parallel-safe expression evaluation. Saving a population uses normal PostgreSQL transactions and table permissions; it does not imply automatic source refresh or shared provider admission.

The application compiler deduplicates repeated Boolean questions across aliases, then derives the required source population for each question. Questions about the same dataset with identical source populations share a scan and request context. Different branch populations remain separate, so unrelated question/row pairs cannot consume calls or make coverage incomplete. A NULL subject removes only its dependent question from dispatch. Proven conjunctive source filters run before evaluation; joins or ambiguous scopes retain the full selected population rather than guessing a smaller one.

For now, source populations are materialized one at a time so each receives the remaining query allowance. Independent rows and questions retain native concurrency within each scan. This barrier is an implementation limitation, not a semantic dependency; moving admission into a shared native scheduler should allow populations to overlap. The application reserves daily request allowance before dispatch in a separate committed transaction. Result assembly waits for the temporary decision relations and uses the same repeatable-read snapshot as source selection.

Before consuming incomplete evidence, the compiler proves a row-local partial-read fragment. Direct semantic projections preserve NULLs; Boolean predicates use SQL's three-valued logic without converting an unresolved value to a match. Population-dependent and NULL-consuming expressions wait for complete evidence. This is a consumer dependency, not a reason to serialize independent evaluation.
