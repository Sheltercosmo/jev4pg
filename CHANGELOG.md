# Changelog

## Unreleased

* PostgreSQL 14–17 builds for the Python deployment and native extension. `SDD_POSTGRES_MAJOR` selects matching container images, development headers and Rust features. CI covers application installation, native execution, upgrades and container recovery on each major. Ordinary view attachments continue to require PostgreSQL 15+; table access and row security remain available on 14. See [version setup](docs/INSTALLATION.md#postgresql-versions).

* Batch evidence reads and linear payload sizing across the shared HTTP operator runtime.

* Native 0.4.0 improves shared scan, plan and embedding execution with bounded least-recently-used caching, policy-aware replay, one-pass response validation and one-time request serialization. Cached frontiers bypass the scheduler and registry lookups.
* Probability projection borrows typed answer distributions. `embedding_distances` prepares one query for up to 512 local comparisons. The extension upgrade preserves evidence and existing grants. See [usage and function coverage](docs/NATIVE_OPTIMIZATION.md).

## 0.8.0 · 22 October 2026

* Native preview 0.3.0 adds `scan_mapped`: per-question context dictionaries, independent request packing, bounded cache reuse across cursor windows and shared evidence with operational receipts. PostgreSQL 0.1.0 and 0.2.0 extension schemas have an upgrade path. Existing scan and application-planner layouts are unchanged. See [usage](docs/NATIVE_RECORD_MAP.md).
* Guided `jev4pg setup` creates a private existing-database connection profile, with separate administrator and runtime settings. `--env-file` selects configuration explicitly.
* `migrate --sql-interface portable` installs asynchronous `jev.*` jobs without extension files on the PostgreSQL host. Existing extension installations remain supported. See [easy setup](docs/EASY_SETUP.md).

* Paired semantic-filtering results: 98% review accuracy versus 92% for the pg-jev request path; 36% lower median review latency. Three-predicate requests fall from 60 to 38. [Protocol and results](docs/benchmarks/SEMANTIC_FILTERING.md).

Application 0.8.0 uses catalog schema 4, SQL queue interface 0.1.0 and optional native extension 0.3.0 preview. Existing portable or extension installations retain their mode. Native 0.1.0 and 0.2.0 installations upgrade to 0.3.0 through the matching migration.

## 0.7.0

This release brings jev4pg closer to everyday PostgreSQL work: browse existing tables, run queries in the background, review changes and recover saved work. The application uses Python by default. The optional Rust extension remains a preview.

### Application

* Browse tables with primary-key pagination, query tabs and a result grid. Review CSV imports before applying them. A runnable [activity monitor](examples/activity_app/README.md) shows how to build an application on the read API.
* Submit [durable SQL jobs](docs/QUERY_JOBS.md) with idempotent retries, cancellation and execution deadlines. The default Compose deployment includes the query worker. Saved results preserve uncertainty, holds and truncation independently of job status.
* Limit PostgreSQL result transfer before application decoding. Reads return a bounded prefix with explicit row or byte limits; reviewed writes lock bounded targets instead of copying the entire table into Python.
* Attach authorized existing PostgreSQL tables and views without importing their rows. Review source manifests before applying or rebinding them. External deployments support verified TLS and bounded connection pools.
* Share bounded planning evidence across independent stages. Feature publication checks source and review changes before making a refreshed result active.
* Use the `jev4pg` package and command. Existing `sdd` and `jevsd-pg` command aliases remain supported.

### Optional native preview

Rust `jev_native.*` functions provide concurrent semantic scans, shared stage DAGs, conditional branches, persistent evidence and probability embeddings. The native Compose overlay builds the extension for PostgreSQL 17 on Linux. Compatible evidence can be reused across connections; stored embedding vectors can be compared without another model call.

Native maintained features and semantic write review are not included. The guarded remote-source protocol is not connected to application queries. See [native boundaries and remaining work](docs/IMPLEMENTATION_PLAN.md).

### Upgrade

The application is version 0.7.0, the catalog schema is version 4, the asynchronous SQL extension remains 0.1.0 and the optional native extension is 0.2.0. Stop the API and workers, back up the database, then run the matching migration. Catalogs 1 and 2 move into `sdd_catalog`; version 3 gains durable query jobs. Existing source rows, credentials and SQL queues are retained.

The Python distribution name changed. Install into a fresh virtual environment instead of layering `jev4pg` over `jevsd-pg`. Compose project and volume identities are unchanged. A committed catalog upgrade has no automatic downgrade; rollback requires the old application and pre-upgrade backup. Follow the [upgrade procedure](docs/INSTALLATION.md#upgrade).

Pending ordinary write previews from v0.6.0 need a fresh preview and approval under the new bounded-target contract. A stale preview is rejected without applying its write.

Background query retention and queue admission limits remain manual operational responsibilities. The release does not claim unrestricted semantic scans, high availability, cross-host recovery qualification or a new model-accuracy result. Use the documented limits and test your workload before production adoption.

## 0.6.0

The packaged application provides the English and Simplified Chinese workspace, query history and corrections, JEV and hybrid planning, 41 semantic operators, reviewed data changes, typed text imports and configurable hosted or local providers.

Deployment includes the Python service, a SQL worker and the asynchronous `jev.*` PostgreSQL interface. It does not include the Rust native extension.
