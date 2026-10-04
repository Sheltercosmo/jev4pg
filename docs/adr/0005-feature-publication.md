# 5. Fence feature publication separately from evaluation

Status: Accepted

## Problem

A refresh evaluates independent rows and then publishes a materialization reference in a later transaction. Successful evaluation alone cannot authorize that publication: source rows or human corrections may have changed, another refresh may have finished first, or a worker may have lost its lease.

## Decision

Capture a publication contract before evaluation. It identifies the dataset, selected feature revisions, review history, assertion set and previous materialization references. Evaluation retains its existing batching, concurrency and evidence reuse.

After evaluation, a short publication transaction checks the contract and rereads the source population. It publishes all selected references together only when the population is complete, the evaluated run remains available and the captured revisions still match. An older refresh cannot overwrite a newer reference. Source deletion must not restore a redacted run.

Imported source tables use a SHARE lock during this transaction, after inference has finished. Ordinary readers continue; writers wait for publication to commit. Definition review, assertions and publication coordinate through the dataset metadata row. Attached sources retain their existing permissions and receive a point-in-time source check, without stronger data-write locks.

Workers must still own an unexpired lease. Publication and the terminal job update share one transaction; losing the lease rolls them back together. A new correction queues another maintained-feature refresh.

The result reports evaluation completeness separately from `publication`. A stale or incomplete attempt returns `NOT_EVALUATED / BLOCKED_BY_DEPENDENCY` for publication with a reason. It retains compatible observations for a later attempt. A known false feature value is still `VALUE`.

## Scope and consequences

The current materialization is a reference to an evaluated run and its source and semantic fingerprints. It is not a persisted SQL column or a promise of continued freshness after external writes. Query execution continues to validate row dependencies before reusing evidence.

This gate currently accepts the Python runtime's content-hash scope. Native maintained features remain incomplete: they need a PostgreSQL-resident source contract, typed values and review overlays before they can use an equivalent publication check. A PostgreSQL transaction-snapshot identifier alone cannot prove that a later source population is unchanged.

No provider work occurs inside the publication transaction. The additional synchronization is local to committing one dataset's references; independent JEV evaluation retains its shared frontier. Full-population rechecks have a database cost, so native generations should compare their source scope in PostgreSQL instead of returning it to Python.
