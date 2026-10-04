# Documentation

Start with [installation](INSTALLATION.md) for the released application or [native Compose](NATIVE_DEPLOYMENT.md) for the Rust preview. For direct SQL use on an existing server, follow the [native build guide](../native/README.md). The default Compose stack uses the Python runtime.

## Use the database

| Guide | Contents |
| --- | --- |
| [Workspace](USER_GUIDE.md) | Queries, corrections, history and data changes. |
| [简体中文指南](zh/USER_GUIDE.md) | 简体中文工作区使用说明。 |
| [Query examples](NL2SQL_EXAMPLES.md) | SQL, expected results and a runnable tutorial. |
| [Query API](NATURAL_LANGUAGE.md) | Planning, review, confirmation and saved queries. |
| [Existing PostgreSQL data](EXISTING_DATA.md) | Attach tables and views without copying rows. |
| [Source manifests](SOURCE_MANIFESTS.md) | Review a source batch, apply atomically and rebind after a restore. |
| [Text imports](TEXT_IMPORT.md) | Row descriptions, typed extraction and import. |
| [Semantic features](SEMANTIC_FEATURES.md) | Reviewed definitions, corrections and refresh. |

## Operators and SQL

| Guide | Contents |
| --- | --- |
| [Capabilities](CAPABILITIES.md) | Select an interface or operator for a task. |
| [Operator guide](JEV_OPERATORS.md) | Authentication, result states, budgets and lifecycle. |
| [Function reference](JEV_FUNCTION_REFERENCE.md) | HTTP operator arguments and native SQL functions. |
| [Queued PostgreSQL interface](POSTGRESQL_INTERFACE.md) | `jev.*` jobs, client access, results and recovery. |
| [Native extension](../native/README.md) | Build, configure and query through `jev_native.*`. |
| [Native plans](NATIVE_PLANS.md) | Shared stages, typed inputs, conditional branches and SQL compilation. |
| [Native embeddings](NATIVE_EMBEDDINGS.md) | Question bases, probability matrices, vectors and distance. |
| [Native evidence](NATIVE_EVIDENCE.md) | Persistent observations, reuse and admission across connections. |
| [Runnable examples](../examples/operators/README.md) | HTTP clients and native SQL scripts. |

## Configure and develop

| Guide | Contents |
| --- | --- |
| [Installation](INSTALLATION.md) | Release deployment, native preview, credentials and backups. |
| [External PostgreSQL](EXTERNAL_POSTGRESQL.md) | Application-only deployment, verified TLS, connection capacity and recovery. |
| [Remote source protocol](REMOTE_POSTGRESQL_PROTOCOL.md) | Development-only federation guards, schema maintenance and two-server qualification. |
| [Native Compose](NATIVE_DEPLOYMENT.md) | Optimized Rust image, registry setup, upgrades and recovery. |
| [Providers](PROVIDERS.md) | TypeSafe, third-party services and local models. |
| [Hybrid queries](HYBRID_QUERY.md) | LLM configuration, context selection and JEV review. |
| [Performance and cost](PERFORMANCE_AND_COST.md) | Usage accounting, tuning and measurement scope. |
| [Architecture](ARCHITECTURE.md) | Components, languages and data boundaries. |
| [Stage placement](JEV_PLANNING_STAGES.md) | Dependencies and reasons for parallel placement. |
| [Roadmap](IMPLEMENTATION_PLAN.md) | Implemented native capabilities and remaining release work. |
| [Changelog](../CHANGELOG.md) | Changes since the packaged release and current version identifiers. |
| [Dependencies](DEPENDENCIES.md) | Libraries and external services. |
| [Contributing](../CONTRIBUTING.md) | Development checks and contribution requirements. |

The running service exposes its OpenAPI reference at `/docs`.
