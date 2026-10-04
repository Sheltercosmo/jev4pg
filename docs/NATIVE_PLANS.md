# Native relational plans

Development interface for relational and semantic execution through `jev_native.execute_plan(plan, options)`. This is part of the unreleased Rust extension. Build and provider configuration are documented in [native execution](../native/README.md).

## Run a plan

An administrator grants `EXECUTE ON FUNCTION jev_native.execute_plan(jsonb,jsonb)` to the caller. The caller also needs temporary-table privileges and ordinary access to the source relations. Run the function inside a REPEATABLE READ or SERIALIZABLE transaction. The [SQL example](../examples/planning/native_plan.sql) evaluates completion descriptions and calculates a count only when membership is resolved.

The plan contains `version: 1`, a `target` stage ID and up to 32 `stages`. Each stage declares `id`, `operator`, `sql` and `columns`. Column declarations include `kind`, `label`, optional `nullable`, `unit` and `lineage`. Supported kinds are `integer`, `number`, `text`, `boolean`, `date`, `datetime`, `json` and `other`; `other` retains a PostgreSQL type without asserting a portable kind. Runtime checks enforce output names, kinds, non-null declarations and unique `keys`. A scalar key is `[]`. Labels, units, lineage descriptions and textual assertions remain descriptive metadata.

`inputs` bind predecessor IDs to SQL aliases: `{"stage":"assessed","alias":"items","require_values":["eligible"]}`. A semantic stage adds typed `questions` to its SQL projection. Its consumers can read `__jev_decisions`, `__jev_observation`, `__jev_receipt` and `__jev_policy` alongside the original typed columns. Project only needed context into a subsequent semantic stage.

The result contains target `rows`, separate `stages` receipts, shared `usage`, the applied `policy` and `completion_order`. A sealed empty target has `VALUE`, zero rows and `population_closed: true`. A held target has `NOT_EVALUATED` and a reason. Decision counts within each stage preserve VALUE, UNKNOWN and NOT_EVALUATED independently of relation availability.

Existing Python `StageDAG` programs can call `sdd.generic.native_plan.native_plan(dag, target, questions=..., requirements=..., guards=..., row_guards=..., selections=..., contexts=...)` to produce this JSON. The adapter lowers executable SQL and typed dependencies. It does not turn descriptive LLM plan steps into executable instructions.

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

### Conditional SQL

Use `CASE` to limit semantic work to the rows that need it. For `messages(id, body, needs_review)`:

```sql
SELECT id,
       CASE WHEN needs_review
            THEN SEMANTIC(body, 'The message requests further action.')
            ELSE false
       END AS follow_up
FROM messages
ORDER BY id;
```

Only rows with `needs_review = true` require a judgment. A SQL NULL condition follows the next WHEN or ELSE, as in PostgreSQL. An UNKNOWN or NOT_EVALUATED semantic condition holds the result; it cannot select ELSE. A missing ELSE produces SQL NULL after all conditions have resolved without a match.

Searched, simple and nested CASE are supported. The compiler materializes the input once, gives each row an internal identity, and joins branch results using that identity. Duplicates remain separate result rows. Internal identities and routing fields are excluded from model context, so identical data can still reuse an observation. Independent questions with the same eligibility share a request; independent branches share the native scheduler.

Exact Boolean conditions can remove unnecessary calls, such as `false AND SEMANTIC(...)` or `true OR SEMANTIC(...)`. Other unresolved combinations are held conservatively. A branch that is never selected can complete with zero model allowance. Selected judgments must resolve before an exact aggregate consumes the result. PostgreSQL performs the final CASE expression and arithmetic.

Project aggregate or window conditions into a preceding CTE. Conditional lowering does not add routing for COALESCE or remove the restrictions on correlated subqueries. The additional intermediate relations count against the shared stage and row limits.

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

A stage can declare `guard: {"input":"items","question":"eligible","equals":true}`. This requires exactly one selected value. Empty inputs are unresolved; multiple rows produce a FAILED stage receipt. Neither implies ANY, ALL or false.

Matching values enable work. A known nonmatching guard leaves the branch `NOT_EVALUATED / SKIPPED`. An unresolved guard produces `NOT_EVALUATED / BLOCKED_BY_DEPENDENCY`; incompatible types produce `BLOCKED_BY_POLICY`. Boolean true and integer 1 are different values. Declared Choice uncertainty options and Score confidence policies must be resolved before checking a guard.

### Route individual rows

A semantic stage can add `row_guard: {"column":"route","equals":true}`. Its SQL projects a single typed decision into a declared JSON column, for example `__jev_decisions->'urgent' AS route`. The guard runs before evidence lookup or provider admission. Only matching rows enter semantic evaluation. The SQL projection itself runs before the guard; use SQL conditions for calculations that must also be conditional.

Every input row remains in the branch result. A known nonmatch produces `NOT_EVALUATED / SKIPPED` for that branch's questions. An unknown or unexecuted selector produces `BLOCKED_BY_DEPENDENCY`. Malformed or missing decisions produce `FAILED`; incompatible scalar types produce `BLOCKED_BY_POLICY`. None supplies a false answer. An empty input remains a sealed empty relation.

The routing column stays in the result but is removed from the provider context. Other projected columns form the context by default. An optional `context_columns: ["body", "account_id"]` restricts context to named projected fields. It must be nonempty, include every question's subject column, and exclude the routing column. The adapter accepts this as `contexts={stage_id: ["body", "account_id"]}`. Fields omitted from context still remain in the stage's SQL result.

This permits identical selected contexts to reuse evidence despite different routing observations or internal identities, without removing duplicate rows. To replay an observation manually, supply exactly the projected context. Skipped rows have no observation or provider receipt.

Use `require_values: []` on the guarded input when the branch is meant to receive unresolved selectors and report their state per row. The default remains whole-input completeness. Independent branches share the existing scheduler, limits and evidence registry.

### Merge selected decisions

A `merge` stage performs local decision selection without a model call. Its SQL combines branch outputs at an explicit row identity and projects the selector and branch decisions into JSON columns. It declares `selections` instead of `questions`:

```json
{
  "answer": {
    "selector": "route",
    "cases": [
      {"equals": true, "column": "urgent_result"},
      {"equals": false, "column": "routine_result"}
    ]
  }
}
```

Each case names a projected decision column. Cases must be distinct Boolean, text or exact numeric values of one type. An optional `otherwise` names a fallback column for a known selector without a matching case. An unresolved selector never activates that fallback. Without a matching case or fallback, the result is `NOT_EVALUATED / BLOCKED_BY_POLICY`.

The selected decision retains its value, uncertainty or operational failure. Unchosen branch states do not affect it. Results appear under `__jev_decisions` just like evaluated questions; a later consumer requires the merged decisions by default. This lets an exact aggregate proceed when every selected answer is resolved, even though each alternative branch contains intentionally skipped work.

Declare merge inputs with `require_values: []` to accept those skipped alternatives. Keep a stable key in each branch and preserve its multiplicity when joining. Key contracts can detect accidental fanout; the executor does not infer the intended identity from arbitrary SQL. Merge stages do not create model observations. Preserve upstream receipt columns in the projection when their row-level provenance is needed.

See the [explicit conditional plan example](../examples/planning/conditional_plan.sql). The query service uses the same row guards and selections when compiling ordinary SQL CASE. Explicit plans remain useful when callers need their own routing, decision types or completeness contracts.

## Snapshot and resource contract

The relational executor requires REPEATABLE READ or SERIALIZABLE before dispatch. Read-write SPI makes completed temporary relations visible to later stages. Under READ COMMITTED it can also refresh external source snapshots, so accepting that isolation level would break the intended source consistency contract.

Materialized rows across all stages share `max_rows`; provider requests, judgments and input bytes share the existing executor limits. Target JSON is capped at 8 MB and fetched in bounded batches. Temporary relations follow PostgreSQL resource controls and are dropped before return, or rolled back on an error. Durable observations and uncertain request attempts retain the registry's separate commit timeline.

Each source must be one SELECT that fits inside a derived table. The function runs with caller privileges; it is not a sandbox for SQL functions or an authorization layer. The query service validates user SQL and binds authorized catalog objects before lowering it. The released Python engine remains the default; automatic native execution requires explicit deployment configuration.

## Acceptance cases

The implementation must demonstrate shared diamond dependencies, duplicate preservation, and a completed small branch releasing its child before a larger independent source finishes. It must also cover semantic filter → aggregate → downstream judgment alongside an independently scoped denominator; aggregate → rank → aggregate; false, uncertain, failed and budget-held guards; empty and multirow guards; changed policies; source updates between stages; cancellation and cleanup. Paraphrases, Simplified Chinese and schema renaming test the contracts without implying language accuracy from deterministic fixtures.

References: [SPI visibility](https://www.postgresql.org/docs/17/spi-visibility.html), [SPI execution](https://www.postgresql.org/docs/17/spi-spi-execute.html).
