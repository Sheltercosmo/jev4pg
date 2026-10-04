# Native semantic execution in PostgreSQL

Status: accepted direction; implementation and evaluation in progress.

The 0.6.0 interface dispatches JSON jobs to Python. Its semantic SQL path copies full source populations into dictionaries, hashes them repeatedly and lowers outcomes into SQL literals. Ordinary queries also perform exact source counts for bookkeeping. These choices couple execution cost to table size even when the requested population is small.

Build a Rust execution module inside PostgreSQL using pgrx. PostgreSQL should filter and project source relations, feed bounded batches to the semantic executor, and join typed outcomes using database keys. The evaluator transport remains replaceable and must retain independent JEV question batches. Python remains the planning, UI and model-adapter layer, rather than the owner of source-row execution.

The first native path uses an invoker-rights PostgreSQL cursor and a relational result function. It establishes snapshot, memory, cancellation and typed-result contracts before optimizer hooks are added. CustomScan integration is justified only after a measured limitation of that interface is reproduced. Avoid an unsafe whole-query rewrite or a per-row model call disguised as an ordinary cheap predicate.

Evidence reuse must include source/context, evaluator and question identity. Native execution cannot silently bypass budgets, role checks, unresolved decisions or mutation review. Query-time model usage is an external effect and is not rolled back with SQL. Maintained semantic features are the preferred path for repeatable transactional reads and writes; explicit on-demand evaluation remains available for exploratory work.

Rust was selected for typed states, bounded concurrent I/O and memory ownership. pgrx supplies PostgreSQL type and memory integration. A separate storage engine would duplicate PostgreSQL transactions, indexing and operations without addressing the observed semantic execution problems. Rewriting the Python planner first would also leave those problems in place.

References: [PostgreSQL custom scans](https://www.postgresql.org/docs/17/custom-scan.html), [pgrx](https://github.com/pgcentralfoundation/pgrx), [PostgreSQL snapshot semantics](https://www.postgresql.org/docs/17/spi-visibility.html).
