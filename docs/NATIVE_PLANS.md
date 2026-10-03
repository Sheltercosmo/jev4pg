# Native relational plans

Design contract for the next native execution step. The executor described here is not yet available. Current SQL entry points are documented in [native execution](../native/README.md).

## One graph for relational and semantic work

Extend the existing typed `StageDAG`. Its executable records carry SQL, input identities, output columns, grain, keys and assertions. Natural-language plan descriptions help users review intent; they are not executable stage contracts.

PostgreSQL owns joins, grouping, windows, sorting and exact arithmetic. A semantic stage adds typed questions to a projected relation. One native executor, query allowance, registry connection and source snapshot span the graph. A shared predecessor is materialized once. Dependent stages become runnable when their own inputs close, while unrelated cursors continue. The existing bounded batch remains a scheduling boundary; a whole-graph layer barrier is unnecessary.

Input aliases bind explicitly to predecessor relations. Generated SQL refers to those aliases, and native execution binds them to private temporary relations. Temporary storage retains row multiplicity and PostgreSQL types. Semantic row observations carry separate lineage to their input rows, upstream evidence and applied policy.

## Input requirements

| Requirement | Consumer contract |
| --- | --- |
| Sealed relation | Input enumeration finished. An empty relation is valid. Unresolved decisions remain explicit. |
| Complete decisions | Named decisions resolved for every eligible input row. Required before exact semantic membership or population-dependent calculation. |
| Selected value | A particular typed upstream decision supplies a context value. UNKNOWN or NOT_EVALUATED blocks dependent work. |

Completeness is scoped to the questions and population the consumer actually needs. A skipped branch does not invalidate an unrelated branch. Every stage has a separate receipt recording whether its population closed, its eligible counts and its operational state. Empty and held stages never invent a data row to carry status.

## Conditions

A row guard follows declared row lineage. A stage guard requires exactly one selected value. Empty inputs are unresolved; multiple rows are a cardinality error. Neither implies ANY, ALL or false.

Matching values enable work. A known nonmatching guard leaves the branch `NOT_EVALUATED / SKIPPED`. An unresolved guard produces `NOT_EVALUATED / BLOCKED_BY_DEPENDENCY`; incompatible types produce `BLOCKED_BY_POLICY`. Boolean true and integer 1 are different values. Declared Choice uncertainty options and Score confidence policies must be resolved before checking a guard.

## Snapshot and resource contract

The first relational executor will require REPEATABLE READ or SERIALIZABLE before dispatch. Read-write SPI makes completed temporary relations visible to later stages. Under READ COMMITTED it can also refresh external source snapshots, so accepting that isolation level would break the intended source consistency contract.

Source rows and provider requests share graph-wide limits. Temporary relations follow PostgreSQL resource controls, and cursor and relation cleanup must work on cancellation, errors and partial result consumption. Durable observations and uncertain request attempts retain the registry's separate commit timeline.

## Acceptance cases

The implementation must demonstrate shared diamond dependencies, duplicate preservation, and a completed small branch releasing its child before a larger independent source finishes. It must also cover semantic filter → aggregate → downstream judgment alongside an independently scoped denominator; aggregate → rank → aggregate; false, uncertain, failed and budget-held guards; empty and multirow guards; changed policies; source updates between stages; cancellation and cleanup. Paraphrases, Simplified Chinese and schema renaming test the contracts without implying language accuracy from deterministic fixtures.

References: [SPI visibility](https://www.postgresql.org/docs/17/spi-visibility.html), [SPI execution](https://www.postgresql.org/docs/17/spi-spi-execute.html).
