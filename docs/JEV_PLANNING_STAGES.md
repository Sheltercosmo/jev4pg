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
