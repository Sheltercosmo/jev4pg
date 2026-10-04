# Domain model

jevsd-pg is a semantic database built on PostgreSQL. Source relations remain relational data. A semantic predicate is a versioned typed question about a subject and its declared context. An observation records what an evaluator returned, rather than replacing source truth with a mutable label.

An evaluator identifies the provider, model and context construction revision. A decision policy resolves an observation into VALUE or UNKNOWN. NOT_EVALUATED means no result is available and is independent of operational status. An incomplete predicate population cannot silently become an exact aggregate or authorize a mutation.

A semantic plan is a shared stage DAG. Independent judgments may execute together; dependencies express actual data requirements. A native execution module owns bounded source batches, typed decisions, admission and relational composition. The planning module interprets user objectives and produces inspectable plans. The development module governs reusable definitions, evidence, corrections and refresh.

PostgreSQL owns storage, types, transactions, authorization, ordinary relational planning and arithmetic. Python owns the current workspace and natural-language planning. The native Rust executor is an opt-in source preview. Release v0.6.0 provides the Python runtime and queued SQL interface; native packaging and production acceptance work are tracked in the roadmap.
