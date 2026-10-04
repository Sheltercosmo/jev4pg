# Query existing PostgreSQL data

Development feature. An attachment registers an existing relation without copying rows or changing ownership. Queries read the source in place. The runtime login needs PostgreSQL access; the catalog determines which logical datasets each workspace tenant can use.

For multiple sources and deployment automation, use a [reviewed source manifest](SOURCE_MANIFESTS.md). It provides metadata-only preview, atomic apply and explicit rebinding without copying data.

## Attach a relation

Run the matching migration first:

```sh
jevsd-pg migrate
```

For native execution, install the Rust extension and use `jevsd-pg migrate --native-interface`. See [native setup](../native/README.md).

A database administrator grants source access:

```sql
GRANT USAGE ON SCHEMA business TO sdd_app;
GRANT SELECT ON business.messages TO sdd_app;
```

From the deployment environment, using its runtime database configuration:

```sh
jevsd-pg attach messages --tenant team --schema business --table messages
```

To select columns and supply a description:

```sh
jevsd-pg attach messages --tenant team --schema business --table messages \
  --column id --column body --column received_at \
  --description "Customer messages and their arrival times"
```

Choose another logical name if `messages` is already registered. Attachment is an operator command, not an HTTP permission granted to reviewers. It exposes the source rows visible to the runtime login under that tenant's PostgreSQL context. Configure source row-security policies first when tenants need different populations. Registration does not invent tenant filters, grant privileges or alter policies.

The dataset appears in `/datasets` and the workspace selector. Query its logical name through `/data/sql`, `/ask` or the CLI:

```sh
jevsd-pg ask "How many messages arrived this month?" --tenant team --dataset messages
```

The SDK equivalent is:

```python
from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService

db = Database(database_url)
dataset = Catalog(db).attach("team", "messages", "business", "messages")
result = SQLService(db).execute("team", "SELECT COUNT(*) AS total FROM messages")
db.engine.dispose()
```

Attachments are read-only through the workspace. Source owners can keep updating their tables; later queries see committed changes without an import. Exact SQL reads do not use the import row limit or copy the source population into Python. Semantic work retains the selected engine's limits.

## Types and relationships

Registration reads types, nullability, comments, primary keys and foreign-key definitions. It does not count or sample rows. Supported columns include integers, decimal and floating-point numbers, text, Boolean values, dates, timestamps, UUIDs and JSON. PostgreSQL retains physical types during execution. Decimal results and UUIDs use strings in the API. Cast UUIDs explicitly when applying text functions.

Select up to 64 columns. Unsupported types are reported explicitly; expose a typed view or select supported columns. Unkeyed relations retain duplicate rows. Native derived semantic plans can evaluate them; the optimized base-column semantic path requires a key.

Foreign keys connect attached targets in the same tenant. Composite relationships retain every column pair in `source_relationships`, and hybrid retrieval preserves their operands. Existing JEV planners that require single-column links receive only compatible single-column relationships. A composite key is never reduced to one column. Metadata records PostgreSQL validation and deferrability.

## Views and snapshots

Tables, partitioned tables, local views and materialized views are supported. Every ordinary view in the dependency chain must use `security_invoker=true`, so PostgreSQL checks the invoking user's permissions and row-security policies. See [PostgreSQL view security](https://www.postgresql.org/docs/17/sql-createview.html).

```sql
CREATE VIEW business.public_messages
WITH (security_invoker=true, security_barrier=true) AS
SELECT id, body, received_at FROM business.messages;
GRANT SELECT ON business.public_messages TO sdd_app;
```

The runtime login also needs access to the underlying invoker-view sources. Materialized views expose their stored snapshot; jevsd-pg does not refresh them or reinterpret their original source policies. Foreign tables and custom types require an explicit supported integration or a local typed representation.

Execution verifies cluster, database and runtime-role identity, relation identity, selected column contracts, keys, comments and view definitions before semantic dispatch. Changed contracts stop the query. Unregistered added columns do not enter the catalog. View definitions are retained as fingerprints rather than copied SQL text. Attachments created without the database identity require an explicit rebind; matching relation OIDs cannot authorize a restored source.

Table locks precede the query's repeatable-read snapshot. Views and materialized views use one additional guard connection for the query's duration. It pins dependencies before the main snapshot starts and closes on success, error or cancellation. A blocking materialized-view refresh waits for these readers. Source row updates remain ordinary PostgreSQL transactions. Include the guard connection in capacity planning.

## Detach or accept a changed schema

```sh
jevsd-pg detach messages --tenant team
```

Detaching removes the dataset from the active catalog and preserves its source relation. Historical results and evidence remain stored. Register again to accept a changed schema; the new dataset identity prevents old planning approvals from silently applying to it. Historical SQL remains inspectable, while execution resolves against the current authorized catalog.

CLI and SDK registration require trusted deployment access. Source grants, definitions and policies remain the database owner's responsibility.
