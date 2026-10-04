# Durable native evidence and request coordination

Status: implemented in the native preview. Production release requirements remain open.

A semantic query can roll back after a provider has returned or charged for work. Storing its request ownership inside the source transaction would erase that history. Repeated queries also need compatible evidence without copying source populations into Python.

Use one restricted PostgreSQL control connection per active native scan invocation. It performs short, separately committed operations against a fixed native registry. Source cursors retain the caller's privileges and snapshot; no control transaction remains open during provider I/O. The initial transport is a local socket or loopback connection. An administrator bounds control connections with the registry login's connection limit and reserves capacity for them.

Keep immutable observations separate from mutable request attempts and admission counters. Derive the evidence scope from the source cluster, database, effective role and application namespace. Match the exact projected context, active typed questions and pinned evaluator. Decision thresholds are excluded, so a new policy can replay the same raw response. The complete raw batch retains each question definition and answer identity for later feature bindings.

Admission commits DISPATCHING, a request token, daily charge and provider slot immediately before HTTP. It does not create a queued lease first. A lost acknowledgment or cancellation can therefore leave an uncertain reservation even if HTTP never started. This is conservative accounting, not a provider invoice. Known cache hits and declined claims consume no new dispatch allowance.

A valid response publishes an immutable observation and closes its current attempt atomically. Repeated publication is idempotent. A failed response is recorded separately from uncertainty about transport. Expired dispatches become UNCERTAIN and retain their slot; expiry alone never authorizes another request. An administrator can reconcile an ended request as CLOSED or RETRY_ALLOWED. That transition fences its old token. A valid late response may still settle an uncertain token that has not been retired.

The source snapshot and evidence timeline are distinct. An on-demand scan can use the newest committed observation that exactly matches its authorized source context and configured age limit, including one committed after the source snapshot began. It returns the observation's original timestamp and receipt. This is not an as-of query over an evidence table. Maintained-feature publication must separately bind the intended source, observation and policy revisions.

Registry helpers are private SECURITY DEFINER functions with a fixed search path. The server-configured registry login receives only their required EXECUTE privileges. Query roles cannot supply registry credentials or write observations directly. Application namespaces distinguish tenants under the existing trusted application role; they do not make a shared database credential safe for hostile clients.

No protocol can infer whether a cancelled external request was billed. Exactly-once external effects require provider-supported idempotency or reconciliation. The native registry prevents automatic redispatch of uncertain work. A persistence error also cannot erase a known VALUE or UNKNOWN: the result keeps its observation and exposes an unconfirmed storage receipt.

Dependent native stages share this registry through one execution plan. Native extension 0.2.0 registers observations, request state, counters and sequences for PostgreSQL backups. Recovery preserves uncertain attempts instead of silently permitting new dispatches.

An external coordinator or background worker may later finish work after a source backend dies or reduce connection pressure. Those changes must preserve the registry's identity, fencing and state contracts. Automatic reuse across differently packed question batches and maintained-feature publication remain separate integration work.

References: [PostgreSQL transaction isolation](https://www.postgresql.org/docs/17/transaction-iso.html), [Tokio PostgreSQL client](https://docs.rs/tokio-postgres/latest/tokio_postgres/).
