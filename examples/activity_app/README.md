# Activity monitor

A runnable read-only application built on jev4pg. It filters and pages through an existing PostgreSQL table while keeping the database API token on the application server. No inference call is needed for ordinary filters or pagination.

## Run locally

Install the development checkout and start a development instance of its API service. Run [schema.sql](schema.sql) in that service's PostgreSQL database as the source owner. It creates a user schema with 10,000 generated events. Source attachments currently use the same PostgreSQL database as the service.

Grant the service's runtime database role `USAGE` on `activity_demo` and `SELECT` on `activity_demo.events`. Keep writes with the source application. Then, using the service's runtime database connection:

```sh
sdd attach activity --tenant demo --schema activity_demo --table events
```

Use the returned dataset ID below. Configure a reader API token for tenant `demo` in the database service, then start this example from the repository root:

```sh
export SDD_API_URL=http://127.0.0.1:8000
export SDD_API_TOKEN=your-reader-token
export SDD_ACTIVITY_DATASET=the-returned-dataset-id
python -m uvicorn examples.activity_app.app:app --host 127.0.0.1 --port 8081
```

On PowerShell, set variables with `$env:SDD_API_URL='http://127.0.0.1:8000'`, and use the same pattern for the token and dataset ID. Open [localhost:8081](http://127.0.0.1:8081).

## What this demonstrates

The backend exposes one constrained application endpoint. It forwards category and minimum-amount filters as typed values, requests only five columns, and returns 50 rows at a time. Next-page reads use the last primary key. An existing connection pool is reused; timeouts return a clear error. The browser renders cell values as text and never receives the service token.

This local example uses one configured reader identity. Add your application's authentication and server-side user-to-tenant mapping before exposing it to other users. Do not accept an arbitrary tenant or service token from a browser query parameter.

Pages are live, not one long snapshot. Inserts, deletes and changes to filter values can affect later pages. Use a fixed database snapshot through a SQL client when exporting a consistent population. Amounts remain decimal strings; parse them with a decimal library for application calculations.

To adapt the example, change the source schema and the backend's explicit field/filter mapping. The database pagination mechanism is independent of those example names. See [large-table and application usage](../../docs/APPLICATIONS.md) for limits, bulk loading and index guidance.
