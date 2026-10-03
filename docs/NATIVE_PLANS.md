# Native relational plans

Development interface for relational and semantic execution through `jev_native.execute_plan(plan, options)`. This is part of the unreleased Rust extension. Build and provider configuration are documented in [native execution](../native/README.md).

## Run a plan

An administrator grants `EXECUTE ON FUNCTION jev_native.execute_plan(jsonb,jsonb)` to the caller. The caller also needs temporary-table privileges and ordinary access to the source relations. Run the function inside a REPEATABLE READ or SERIALIZABLE transaction. The [SQL example](../examples/planning/native_plan.sql) evaluates completion descriptions and calculates a count only when membership is resolved.

The plan contains `version: 1`, a `target` stage ID and up to 32 `stages`. Each stage declares `id`, `operator`, `sql` and `columns`. Column declarations include `kind`, `label`, optional `nullable`, `unit` and `lineage`. Supported kinds are `integer`, `number`, `text`, `boolean`, `date`, `datetime`, `json` and `other`; `other` retains a PostgreSQL type without asserting a portable kind. Runtime checks enforce output names, kinds, non-null declarations and unique `keys`. A scalar key is `[]`. Labels, units, lineage descriptions and textual assertions remain descriptive metadata.

`inputs` bind predecessor IDs to SQL aliases: `{"stage":"assessed","alias":"items","require_values":["eligible"]}`. A semantic stage adds typed `questions` to its SQL projection. Its consumers can read `__jev_decisions`, `__jev_observation`, `__jev_receipt` and `__jev_policy` alongside the original typed columns. Project only needed context into a subsequent semantic stage.

The result contains target `rows`, separate `stages` receipts, shared `usage`, the applied `policy` and `completion_order`. A sealed empty target has `VALUE`, zero rows and `population_closed: true`. A held target has `NOT_EVALUATED` and a reason. Decision counts within each stage preserve VALUE, UNKNOWN and NOT_EVALUATED independently of relation availability.

Existing Python `StageDAG` programs can call `sdd.generic.native_plan.native_plan(dag, target, questions=..., requirements=..., guards=...)` to produce this JSON. The adapter lowers executable SQL and typed dependencies. It does not turn descriptive LLM plan steps into executable instructions.

## Use generated or handwritten SQL

With the native extension installed and `SDD_SEMANTIC_ENGINE=native`, the query service lowers `SEMANTIC` on CTE and derived-table columns into the same DAG. SQL authorization and catalog binding happen before compilation. This applies to SQL submitted directly, produced by the natural-language planner or edited from query history.

For a registered table `messages(account_id, body)`, judge each account's combined messages:

```sql
WITH account_text AS (
    SELECT account_id, STRING_AGG(body, E'\n') AS description
    FROM messages
    GROUP BY account_id
)
SELECT account_id
FROM account_text
WHERE SEMANTIC(description, 'The messages describe an unresolved request for help.');
```

PostgreSQL produces the account descriptions before JEV evaluates them. A second semantic stage can consume a SQL calculation over earlier resolved decisions. Shared CTEs materialize once; independent semantic branches share native scheduling and query limits. Project the columns needed for each judgment into its CTE: those projected values become its context. Exact arithmetic stays in PostgreSQL.

This path accepts uncorrelated relational stages, including joins, aggregates, windows and set operations. Put expressions in a preceding SELECT and give them names before using them as semantic subjects. Recursive CTEs, correlated or scalar subqueries, maintained features and semantic writes are not supported here. Existing keyed base-column predicates retain their optimized scan path.

An exact consumer waits for every required input decision. Missing evidence holds the target with `NOT_EVALUATED`; it does not return a zero count. The query response and saved history retain the proposed SQL, stage receipts, usage, applied policy and hold reason. The web workspace distinguishes a held query from an executed query with no matching rows. The generation and review contracts describe the configured engine's capabilities; compiler checks do not establish natural-language accuracy.

The query service preserves decimal JSON tokens as exact strings in returned rows and history, matching its numeric serialization convention. Provider usage and policy metadata remain separate from result values.

## One graph for relational and semantic work

The existing typed `StageDAG` supplies SQL, input identities, output columns, grain, keys and assertions. Natural-language plan descriptions help users review intent; they are not executable stage contracts.

PostgreSQL owns joins, grouping, windows, sorting and exact arithmetic. A semantic stage adds typed questions to a projected relation. One native executor, query allowance, registry connection and source snapshot span the graph. A shared predecessor is materialized once. Dependent stages become runnable when their own inputs close, while unrelated cursors continue. The existing bounded batch remains a scheduling boundary; a whole-graph layer barrier is unnecessary.

Input aliases bind explicitly to predecessor relations. Generated SQL refers to those aliases, and native execution binds them to private temporary relations. Temporary storage retains row multiplicity and PostgreSQL types. Row ordinals associate each semantic result with its materialized input. Observations and applied policies remain separate from source values. Explicit provenance across arbitrary joins and projections is a remaining gate.

## Input requirements

| Requirement | Consumer contract |
| --- | --- |
| Sealed relation | Input enumeration finished. An empty relation is valid. Unresolved decisions remain explicit. |
| Complete decisions | Named decisions resolved for every eligible input row. Required before exact semantic membership or population-dependent calculation. |
| Selected value | A particular typed upstream decision supplies a context value. UNKNOWN or NOT_EVALUATED blocks dependent work. |

Omitting `require_values` requires all declared decisions of that predecessor. An explicit list limits completeness to those questions; `[]` explicitly accepts a sealed relation with unresolved decisions, for example an independently scoped denominator. Use `jev_native.require_bool` when a decision determines exact membership. Completeness is currently checked over the whole declared input relation. A narrower eligible population should be its own stage.

Every stage has a separate receipt recording whether its population closed, its row and decision counts, and its operational state. Empty and held stages never invent a data row to carry status. A held predecessor blocks its consumers; independent branches continue.

## Conditions

A stage can declare `guard: {"input":"items","question":"eligible","equals":true}`. This requires exactly one selected value. Empty inputs are unresolved; multiple rows produce a FAILED stage receipt. Neither implies ANY, ALL or false. Row guards and combining selected alternatives are not implemented yet.

Matching values enable work. A known nonmatching guard leaves the branch `NOT_EVALUATED / SKIPPED`. An unresolved guard produces `NOT_EVALUATED / BLOCKED_BY_DEPENDENCY`; incompatible types produce `BLOCKED_BY_POLICY`. Boolean true and integer 1 are different values. Declared Choice uncertainty options and Score confidence policies must be resolved before checking a guard.

## Snapshot and resource contract

The relational executor requires REPEATABLE READ or SERIALIZABLE before dispatch. Read-write SPI makes completed temporary relations visible to later stages. Under READ COMMITTED it can also refresh external source snapshots, so accepting that isolation level would break the intended source consistency contract.

Materialized rows across all stages share `max_rows`; provider requests, judgments and input bytes share the existing executor limits. Target JSON is capped at 8 MB and fetched in bounded batches. Temporary relations follow PostgreSQL resource controls and are dropped before return, or rolled back on an error. Durable observations and uncertain request attempts retain the registry's separate commit timeline.

Each source must be one SELECT that fits inside a derived table. The function runs with caller privileges; it is not a sandbox for SQL functions or an authorization layer. The query service validates user SQL and binds authorized catalog objects before lowering it. The released Python engine remains the default; automatic native execution requires explicit deployment configuration.

## Acceptance cases

The implementation must demonstrate shared diamond dependencies, duplicate preservation, and a completed small branch releasing its child before a larger independent source finishes. It must also cover semantic filter → aggregate → downstream judgment alongside an independently scoped denominator; aggregate → rank → aggregate; false, uncertain, failed and budget-held guards; empty and multirow guards; changed policies; source updates between stages; cancellation and cleanup. Paraphrases, Simplified Chinese and schema renaming test the contracts without implying language accuracy from deterministic fixtures.

References: [SPI visibility](https://www.postgresql.org/docs/17/spi-visibility.html), [SPI execution](https://www.postgresql.org/docs/17/spi-spi-execute.html).
