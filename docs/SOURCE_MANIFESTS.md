# Review and adopt existing sources

A source manifest registers multiple PostgreSQL tables or views in one transaction. Preview discovers their column contracts, keys and relationships. Apply rechecks that metadata before publishing attachments. Neither operation imports rows or changes source permissions, policies or ownership.

Use the deployment runtime login with the source grants described in [existing data](EXISTING_DATA.md). These are trusted operator commands; an HTTP reviewer token does not grant source-registration access.

## Define the sources

Start with [the example manifest](../examples/sources/manifest.json) and replace its schema, relation and column names. The relations must already exist.

```json
{
  "version": 1,
  "sources": [
    {
      "name": "messages",
      "schema": "business",
      "table": "messages",
      "columns": ["id", "body", "received_at"],
      "description": "Messages available to this workspace"
    }
  ]
}
```

`name` is the logical dataset name used in queries. `schema` and `table` identify the physical relation. Omit `columns` to select all supported columns; an explicit list prevents later columns from entering the attachment. Omit `description` to use the PostgreSQL relation comment. Primary keys and foreign keys come from PostgreSQL metadata, including every column of a composite key. Views without declared keys remain unkeyed; duplicates and NULLs retain their ordinary SQL behavior.

A manifest accepts 1–256 sources and 1–64 selected columns per source. CLI input files are limited to 16 MiB. Logical names must be unique ignoring letter case. Unknown fields are rejected. The existing [type and view contracts](EXISTING_DATA.md#types-and-relationships) apply.

## Preview, review and apply

```sh
jev4pg sources preview sources.json --tenant team --output sources.plan.json
jev4pg sources apply sources.plan.json --tenant team
```

Preview runs in a read-only transaction and inspects metadata without enumerating or sampling business rows. It writes a new JSON plan and refuses to overwrite an existing file. Review each source's selected columns, key, description, database identity and proposed action. `relationships` shows relationships among the sources in this manifest. Other already attached targets remain available through the catalog.

| Action | Effect of apply |
| --- | --- |
| `create` | Publish a new read-only attachment with the proposed dataset ID. |
| `keep` | Retain the existing attachment and ID. |
| `rebind` | Retire the reviewed old attachment and publish a new ID. Source data and historical records remain intact. |
| `conflict` | No apply is allowed. Correct the manifest and preview again. |

A preview containing conflicts still writes its review file, reports `blocked` and exits nonzero. An imported dataset is always a conflict; a manifest cannot replace its data.

Apply checks the tenant, cluster, database, runtime role, source contracts and affected catalog entries again. A changed source or catalog entry requires a new preview. The batch commits together; a failure rolls back every attachment change. Reapplying the same successful plan retains its dataset IDs, including when two processes submit it concurrently.

The plan fingerprint detects accidental edits. It is not an authorization signature: access comes from the trusted deployment environment. Edit the manifest and create a new review file instead of editing the plan.

## Accept schema drift or a restored source

Set `"rebind": true` on the affected manifest entry and generate a new preview. This permits a changed attachment to be replaced after review. Matching attachments still produce `keep`. A rebind uses a new dataset ID so old planning approvals cannot silently inherit the new source contract.

Attachments record PostgreSQL's cluster identifier, database OID and runtime-role OID alongside relation and column identities. A reused relation OID alone cannot establish continuity. Legacy attachments without this identity are blocked until explicitly rebound. The runtime normally reads the identity through `pg_catalog.pg_control_system()`; installations that revoke access must grant this specific function before using attachments.

After a logical restore, stop application and maintenance processes, verify source data and grants, then preview and apply the necessary rebindings under the restored runtime login. This does not migrate remote data or prove high availability. Physical replicas share cluster lineage; automatic failover and branched physical copies still need their own operational qualification. See [external PostgreSQL recovery](EXTERNAL_POSTGRESQL.md#recovery-and-existing-sources).

## Python interface

```python
from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.source_manifest import SourceOnboarding

db = Database(database_url)
try:
    onboarding = SourceOnboarding(Catalog(db))
    plan = onboarding.preview("team", manifest)
    # Persist and review the plan before applying it.
    result = onboarding.apply("team", plan)
finally:
    db.engine.dispose()
```

No provider is required and no JEV or LLM calls occur during onboarding. Query execution uses the same source validation and shared stage DAG afterward.
