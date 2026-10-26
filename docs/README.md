# Documentation

Start with the [quick start](../README.md#quick-start), then choose the guide for your workflow:

| You want | Guide |
| --- | --- |
| Workspace, natural-language queries and HTTP APIs | [Python application deployment](INSTALLATION.md#docker-compose) |
| The application or queued operators beside an existing database | [Guided Python setup](EASY_SETUP.md) |
| Synchronous semantic SQL without a Python service | [Native Rust extension](../native/README.md) |
| The workspace with native semantic reads | [Combined Compose deployment](NATIVE_DEPLOYMENT.md) |

These guides use current `main` for PostgreSQL 14–17. The [capability map](CAPABILITIES.md#python-and-native-roles) explains each component's role. PostgreSQL stores data and executes relational SQL in every setup. [Installation](INSTALLATION.md) covers version selection, prerequisites, credentials and upgrades.

## Use the database

| Guide | Contents |
| --- | --- |
| [Workspace](USER_GUIDE.md) | Queries, corrections, history and data changes. |
| [Large tables and applications](APPLICATIONS.md) | Cursor pagination, bulk loading, limits and a runnable application example in v0.7.0. |
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
| [Semantic dictionaries](NATIVE_RECORD_MAP.md) | Mapped SQL windows, independent batching and continuous input caching. |
| [Runnable examples](../examples/operators/README.md) | HTTP clients and native SQL scripts. |

## Configure and develop

| Guide | Contents |
| --- | --- |
| [Installation](INSTALLATION.md) | Release deployment, native preview, credentials and backups. |
| [External PostgreSQL](EXTERNAL_POSTGRESQL.md) | Application-only deployment, verified TLS, connection capacity and recovery. |
| [Remote source protocol](REMOTE_POSTGRESQL_PROTOCOL.md) | Development-only federation guards, schema maintenance and two-server qualification. |
| [Combined Compose deployment](NATIVE_DEPLOYMENT.md) | Workspace with native semantic reads, registry setup, upgrades and recovery. |
| [Native optimization](NATIVE_OPTIMIZATION.md) | Shared caching, request preparation and batch embedding comparison in native 0.4.0 development. |
| [Providers](PROVIDERS.md) | TypeSafe, third-party services and local models. |
| [Hybrid queries](HYBRID_QUERY.md) | LLM configuration, context selection and JEV review. |
| [Planning context](PLANNING_CONTEXT.md) | Bounded source samples, indexed literal lookup, completeness and parallel collection. |
| [Query controls](QUERY_CONTROL.md) | Application cancellation handles, execution deadlines and retained query history. |
| [Query jobs](QUERY_JOBS.md) | Durable SQL submission, idempotent retries, workers, cancellation and recovery. |
| [Performance and cost](PERFORMANCE_AND_COST.md) | Usage accounting, tuning and measurement scope. |
| [Semantic filtering comparison](benchmarks/SEMANTIC_FILTERING.md) | Native 0.3.0 versus the pg-jev request path: accuracy, latency, tokens and continuous reuse. |
| [BIRD Challenging comparison](benchmarks/BIRD_CHALLENGING_100.md) | Historical 100-question comparison of JEV, GPT-5.6 Terra and hybrid planning. |
| [Architecture](ARCHITECTURE.md) | Components, languages and data boundaries. |
| [Stage placement](JEV_PLANNING_STAGES.md) | Dependencies and reasons for parallel placement. |
| [Roadmap](IMPLEMENTATION_PLAN.md) | Implemented native capabilities and remaining release work. |
| [Changelog](../CHANGELOG.md) | Release changes and component compatibility. |
| [Dependencies](DEPENDENCIES.md) | Libraries and external services. |
| [Contributing](../CONTRIBUTING.md) | Development checks and contribution requirements. |

The running service exposes its OpenAPI reference at `/docs`.
