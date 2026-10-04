# Documentation

Start with installation, then use the workspace guide or API examples for your task.

| Guide | Contents |
| --- | --- |
| [Installation](INSTALLATION.md) | Python, PostgreSQL, credentials and service setup. |
| [Existing data](EXISTING_DATA.md) | Attach PostgreSQL tables and views without importing their rows. |
| [PostgreSQL interface](POSTGRESQL_INTERFACE.md) | SQL login setup, operator jobs, results and recovery. |
| [Workspace](USER_GUIDE.md) | Queries, corrections, history and data changes. |
| [简体中文指南](zh/USER_GUIDE.md) | 简体中文工作区使用说明。 |
| [SQL examples](NL2SQL_EXAMPLES.md) | Verified queries, results and a runnable tutorial. |
| [Query API](NATURAL_LANGUAGE.md) | Planning, review, confirmation and saved queries. |
| [JEV providers](PROVIDERS.md) | TypeSafe, third-party services and local models. |
| [Hybrid queries](HYBRID_QUERY.md) | LLM configuration, context selection and JEV review. |
| [Text imports](TEXT_IMPORT.md) | Row descriptions, typed extraction and import. |
| [Semantic features](SEMANTIC_FEATURES.md) | Reviewed definitions, corrections and refresh. |
| [Capabilities](CAPABILITIES.md) | Which API or operator to use for a task. |
| [Operator guide](JEV_OPERATORS.md) | Authentication, result states, budgets and lifecycle. |
| [Function reference](JEV_FUNCTION_REFERENCE.md) | Every operator's arguments, usage and result. |
| [Performance and cost](PERFORMANCE_AND_COST.md) | Release measurements and their limits. |
| [Architecture](ARCHITECTURE.md) | Components, stage dependencies and parallel work. |
| [Native plans](NATIVE_PLANS.md) | Development Rust/PostgreSQL stage execution, typed inputs and SQL usage. |
| [Native embeddings](NATIVE_EMBEDDINGS.md) | Multi-question probability matrices, reusable vectors and SQL similarity queries. |
| [Dependencies](DEPENDENCIES.md) | Libraries and external services. |

The running service exposes OpenAPI documentation at `/docs`. Runnable operator clients are in [examples/operators](../examples/operators/README.md). See [contribution guidance](../CONTRIBUTING.md) to change the code or documentation.
