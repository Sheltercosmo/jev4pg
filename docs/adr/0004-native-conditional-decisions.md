# Conditional native decisions

## Decision

Represent conditional semantic work as row-preserving stages in the shared DAG. A typed selector decides whether a row's questions enter evaluation. A separate merge stage chooses the applicable decision without making another model request.

Known nonmatches produce NOT_EVALUATED / SKIPPED. Unknown or unexecuted selectors block dependent questions. A selected branch retains its own value, uncertainty or operational failure. Neither routing nor merging converts absence of work into false.

## Placement

SQL first establishes the projected relation and selector column. Rust checks each row condition before evidence lookup and admission. Selected contexts exclude the routing column, allowing reuse based on the actual question context. Independent branches share the existing scheduler and query allowance; they do not become a serial tree.

A merge waits for its declared predecessor relations. Its SQL aligns branch records; local selection reads only the chosen decision. Explicitly accepting unresolved predecessor decisions lets the merge inspect intentionally skipped alternatives. Default downstream completeness then checks the merged answers before an exact aggregate or dependent judgment.

## Identity and evidence

Rows retain their stage ordinal, including duplicates, missing selectors and unselected work. A merge's SQL must preserve the intended row identity and multiplicity; declared keys can detect fanout but do not prove an arbitrary join correct. Raw observations remain with their evaluated branches. A local selection creates no model observation or provider receipt.

Caller-supplied typed decisions are assertions, not authenticated model evidence. Existing SQL privileges and the observation registry remain the trust boundaries. Automatic query compilation must construct routing and row alignment from authorized expressions and proven scope.

## Costs and remaining work

Branches retain projected rows even when their questions are skipped. This uses intermediate storage but preserves coverage and inspection. Existing row, context and result limits continue to apply. Conditions save provider work; they do not suppress the stage's preceding SQL calculation.

The explicit plan and adapter contracts precede automatic SQL conditional lowering. Combining whole-stage skipped alternatives and inferring provenance across arbitrary joins require additional contracts. These mechanisms must be validated without weakening unresolved membership checks or serializing independent work.
