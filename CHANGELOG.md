# Changelog

## 0.7.0.dev0 — development preview

These changes are available from `main`. The packaged application release remains [v0.6.0](https://github.com/Sheltercosmo/jev4pg/releases/tag/v0.6.0).

| Feature | What it adds |
| --- | --- |
| [Rust execution](native/README.md) | Call semantic scans directly from PostgreSQL. Independent questions and sources share concurrent scheduling and budgets. |
| [Stage plans](docs/NATIVE_PLANS.md) | Combine SQL, semantic work and conditional branches in a shared DAG. Supported application queries compile into these plans. |
| [Probability embeddings](docs/NATIVE_EMBEDDINGS.md) | Turn a fixed question basis into named answer probabilities, store matrices and compare compatible vectors without more model calls. |
| [Persistent evidence](docs/NATIVE_EVIDENCE.md) | Reuse compatible observations across connections and coordinate provider admission. Decision thresholds remain separate from observations. |
| [Source attachments](docs/EXISTING_DATA.md) | Query authorized PostgreSQL tables and views without importing their rows. Attachments remain read-only through the application. |
| [Native deployment](docs/NATIVE_DEPLOYMENT.md) | Build the extension with Docker Compose and provision restricted registry access. Native 0.2.0 adds registry backup support and an upgrade from 0.1.0. |

The application reports `0.7.0.dev0` through the package, CLI, health endpoint and OpenAPI document. Its catalog schema is version 2; the native extension is version 0.2.0. See [upgrade instructions](docs/INSTALLATION.md#upgrade).

Python remains the default semantic engine. Native execution is a PostgreSQL 17 preview; maintained features and semantic write review still use Python. Current native tests establish execution behavior, not a measured language-accuracy or performance advantage. See the [roadmap](docs/IMPLEMENTATION_PLAN.md).

## 0.6.0

The packaged application provides the English and Simplified Chinese workspace, query history and corrections, JEV and hybrid planning, 41 semantic operators, reviewed data changes, typed text imports and configurable hosted or local providers.

Deployment includes the Python service, a SQL worker and the asynchronous `jev.*` PostgreSQL interface. It does not include the Rust native extension.
