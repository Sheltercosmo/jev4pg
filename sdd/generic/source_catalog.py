"""Attach PostgreSQL relations without copying rows or taking ownership."""

from contextlib import contextmanager, ExitStack

from sqlalchemy import insert, select, text, update

from ..bootstrap import identifier
from ..ledger import digest, now, uid
from . import schema


KINDS = {
    16: "boolean",
    20: "integer",
    21: "integer",
    23: "integer",
    700: "number",
    701: "number",
    1700: "number",
    25: "text",
    1042: "text",
    1043: "text",
    1082: "date",
    1114: "datetime",
    1184: "datetime",
    114: "json",
    3802: "json",
    2950: "text",
}


def qualified(connection, schema_name, table_name):
    return identifier(connection, schema_name) + "." + identifier(connection, table_name)


def lock_sources(connection, datasets):
    """Acquire DDL locks before the first repeatable-read snapshot is established."""
    names = sorted(
        {
            (dataset["schema_name"], dataset["table_name"])
            for dataset in datasets
            if dataset.get("source_binding", {}).get("kind") in {"r", "p"}
        }
    )
    if names:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '3000ms'")
    for schema_name, table_name in names:
        relation = qualified(connection, schema_name, table_name)
        connection.exec_driver_sql(f"LOCK TABLE {relation} IN ACCESS SHARE MODE")


@contextmanager
def source_transaction(db, tenant, datasets, isolation_level=None):
    """Pin indirect relations before opening the query's data snapshot."""
    indirect = [
        dataset
        for dataset in datasets
        if dataset.get("source_binding", {}).get("kind") in {"v", "m"}
    ]
    with ExitStack() as stack:
        if indirect:
            from ..db import Database

            guard_db = Database(db.engine.url)
            stack.callback(guard_db.engine.dispose)
            guard = stack.enter_context(guard_db.transaction(tenant))
            guard.exec_driver_sql("SET LOCAL lock_timeout = '3000ms'")
            guard.exec_driver_sql("SET LOCAL statement_timeout = '10000ms'")
            validate_sources(
                guard, sorted(indirect, key=lambda item: (item["schema_name"], item["table_name"]))
            )
        connection = stack.enter_context(
            db.transaction(
                tenant,
                isolation_level=isolation_level,
                before_snapshot=lambda current: lock_sources(current, datasets),
            )
        )
        validate_sources(connection, datasets)
        yield connection


def internal_schema(schema_name):
    return schema_name.startswith("pg_") or schema_name in {
        "information_schema",
        "sdd_catalog",
        "sdd_data",
        "jev",
        "jev_native",
    }


def inspect_source(connection, schema_name, table_name, names=None):
    if connection.dialect.name != "postgresql":
        raise ValueError("Source attachments require PostgreSQL")
    relation = qualified(connection, schema_name, table_name)
    if internal_schema(schema_name):
        raise ValueError("Attach a user source schema, not an internal schema")
    connection.exec_driver_sql(f"SELECT 1 FROM {relation} LIMIT 0").close()
    info = (
        connection.execute(
            text("""
        SELECT c.oid::bigint AS oid,c.relkind AS kind,c.reloptions AS options,
               obj_description(c.oid,'pg_class') AS description,
               CASE WHEN c.relkind='v' THEN pg_get_viewdef(c.oid,true) END AS view_sql
        FROM pg_class c WHERE c.oid=to_regclass(:relation)
    """),
            {"relation": relation},
        )
        .mappings()
        .one()
    )
    if info["kind"] not in {"r", "p", "v", "m"}:
        raise ValueError("Attach a table, partitioned table, view or materialized view")
    if info["kind"] == "v" and not any(
        option in {"security_invoker=true", "security_invoker=on", "security_invoker=1"}
        for option in (info["options"] or [])
    ):
        raise ValueError("Attached views require security_invoker=true")
    dependencies = (
        connection.execute(
            text("""WITH RECURSIVE relations(oid) AS (
        SELECT CAST(:oid AS oid)
        UNION
        SELECT d.refobjid FROM relations parent JOIN pg_class p ON p.oid=parent.oid
        JOIN pg_rewrite r ON r.ev_class=p.oid
        JOIN pg_depend d ON d.classid='pg_rewrite'::regclass AND d.objid=r.oid
        WHERE p.relkind='v' AND d.refclassid='pg_class'::regclass AND d.refobjid<>p.oid
    ) SELECT c.oid::bigint AS oid,c.relkind AS kind,n.nspname AS schema_name,
        CASE WHEN c.relkind='v' THEN pg_get_viewdef(c.oid,true) END AS view_sql,
        CASE WHEN c.relkind='v' THEN c.reloptions ELSE NULL END AS options
        FROM relations JOIN pg_class c USING(oid)
        JOIN pg_namespace n ON n.oid=c.relnamespace ORDER BY c.oid"""),
            {"oid": info["oid"]},
        )
        .mappings()
        .all()
    )
    for dependency in dependencies:
        if internal_schema(dependency["schema_name"]):
            raise ValueError("Source views must not depend on internal schemas")
        if dependency["kind"] not in {"r", "p", "v", "m"}:
            raise ValueError("Source view dependencies must be local tables or views")
        if dependency["kind"] == "v" and not any(
            option in {"security_invoker=true", "security_invoker=on", "security_invoker=1"}
            for option in (dependency["options"] or [])
        ):
            raise ValueError("Every attached view dependency requires security_invoker=true")
    rows = (
        connection.execute(
            text("""
        SELECT a.attname AS name,a.attnum AS position,a.atttypid::bigint AS type_oid,
               a.atttypmod AS modifier,a.attcollation::bigint AS collation,
               NOT a.attnotnull AS nullable,format_type(a.atttypid,a.atttypmod) AS database_type,
               col_description(a.attrelid,a.attnum) AS description
        FROM pg_attribute a WHERE a.attrelid=:oid AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY a.attnum
    """),
            {"oid": info["oid"]},
        )
        .mappings()
        .all()
    )
    if names is not None:
        if len(names) != len(set(names)) or set(names) - {row["name"] for row in rows}:
            raise ValueError("Attachment columns must be distinct existing names")
        rows = [row for row in rows if row["name"] in names]
    if not 1 <= len(rows) <= 64:
        raise ValueError("Select 1–64 source columns for an attachment")
    columns = []
    for row in rows:
        kind = KINDS.get(row["type_oid"])
        if kind is None:
            raise ValueError(
                f"Unsupported source type for {row['name']}: {row['database_type']}; expose a typed view or select other columns"
            )
        if row["name"].startswith(("_sdd", "__jev_")):
            raise ValueError("Reserved source column name; alias it in a view")
        columns.append(
            {
                "name": row["name"],
                "type": kind,
                "nullable": row["nullable"],
                "description": row["description"] or "",
                "database_type": row["database_type"],
                "native_kind": "other" if row["type_oid"] == 2950 else kind,
            }
        )
    constraints = (
        connection.execute(
            text("""
        SELECT conname AS name,contype AS kind,conkey AS columns,confkey AS target_columns,
               confrelid::bigint AS target_oid,convalidated AS validated,condeferrable AS deferrable
        FROM pg_constraint WHERE conrelid=:oid AND contype IN ('p','f') ORDER BY conname
    """),
            {"oid": info["oid"]},
        )
        .mappings()
        .all()
    )
    positions = {row["position"]: row["name"] for row in rows}
    primary_key, foreign_keys = [], []
    for constraint in constraints:
        if not set(constraint["columns"]) <= positions.keys():
            continue
        source_columns = [positions[index] for index in constraint["columns"]]
        if constraint["kind"] == "p":
            primary_key = source_columns
        else:
            foreign_keys.append(
                {
                    "name": constraint["name"],
                    "source_columns": source_columns,
                    "target_oid": constraint["target_oid"],
                    "target_positions": constraint["target_columns"],
                    "validated": constraint["validated"],
                    "deferrable": constraint["deferrable"],
                }
            )
    projection = ",".join(identifier(connection, column["name"]) for column in columns)
    connection.exec_driver_sql(f"SELECT {projection} FROM {relation} LIMIT 0").close()
    definition = {
        "oid": info["oid"],
        "kind": info["kind"],
        "view_definition": digest(info["view_sql"]) if info["view_sql"] else None,
        "description": info["description"] or "",
        "columns": [
            {
                key: row[key]
                for key in (
                    "name",
                    "position",
                    "type_oid",
                    "modifier",
                    "collation",
                    "nullable",
                    "description",
                )
            }
            for row in rows
        ],
        "primary_key": primary_key,
        "foreign_keys": foreign_keys,
        "relations": [
            {
                "oid": dependency["oid"],
                "kind": dependency["kind"],
                "view_definition": digest(dependency["view_sql"])
                if dependency["view_sql"]
                else None,
                "options": dependency["options"],
            }
            for dependency in dependencies
        ],
    }
    return definition, columns, info["description"] or ""


def validate_sources(connection, datasets):
    for dataset in datasets:
        expected = dataset.get("source_binding")
        if not expected:
            continue
        current = (
            connection.execute(
                select(schema.source_bindings).where(
                    schema.source_bindings.c.dataset_id == dataset["id"],
                    schema.source_bindings.c.tenant == dataset["tenant"],
                    schema.source_bindings.c.active == 1,
                )
            )
            .mappings()
            .first()
        )
        if current is None or current["definition"] != expected:
            raise ValueError("Source attachment changed; load the catalog again")
        actual, _, _ = inspect_source(
            connection,
            dataset["schema_name"],
            dataset["table_name"],
            [column["name"] for column in dataset["columns"]],
        )
        if actual != expected:
            raise ValueError("Source schema changed; detach and register it again before querying")


def attach(catalog, tenant, name, schema_name, table_name, columns=None, description=None):
    if not name.strip() or len(name) > 120 or name.startswith("_sdd"):
        raise ValueError("Invalid dataset name")
    with catalog.db.transaction(tenant) as connection:
        if connection.dialect.name != "postgresql":
            raise ValueError("Source attachments require PostgreSQL")
        connection.execute(text("SET LOCAL statement_timeout = '10000ms'"))
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": tenant + ":datasets"},
        )
        if any(
            value.casefold() == name.casefold()
            for value in connection.execute(
                select(schema.datasets.c.name).where(schema.datasets.c.tenant == tenant)
            ).scalars()
        ):
            raise ValueError("Dataset names must be unique ignoring letter case")
        definition, fields, comment = inspect_source(connection, schema_name, table_name, columns)
        identity = uid()
        record = {
            "id": identity,
            "tenant": tenant,
            "name": name,
            "description": comment if description is None else description,
            "schema_name": schema_name,
            "table_name": table_name,
            "columns": fields,
            "primary_key": definition["primary_key"],
            "writable": 0,
            "links": [],
            "created_at": now(),
        }
        connection.execute(insert(schema.datasets).values(**record))
        connection.execute(
            insert(schema.source_bindings).values(
                id=uid(),
                tenant=tenant,
                dataset_id=identity,
                definition=definition,
                active=1,
                created_at=now(),
            )
        )
    return catalog.get(tenant, identity)


def detach(catalog, tenant, identity):
    dataset = catalog.get(tenant, identity)
    if not dataset.get("source_binding"):
        raise ValueError("This dataset is imported, not attached")
    with catalog.db.transaction(tenant) as connection:
        connection.execute(
            update(schema.source_bindings)
            .where(
                schema.source_bindings.c.dataset_id == dataset["id"],
                schema.source_bindings.c.tenant == tenant,
            )
            .values(active=0)
        )
        connection.execute(
            update(schema.datasets)
            .where(schema.datasets.c.id == dataset["id"], schema.datasets.c.tenant == tenant)
            .values(name="_sdd_detached_" + dataset["id"])
        )
    return {"dataset_id": dataset["id"], "detached": True, "source_preserved": True}


def describe_relationships(datasets):
    by_oid = {}
    for dataset in datasets:
        if dataset.get("source_binding"):
            by_oid.setdefault(dataset["source_binding"]["oid"], []).append(dataset)
    for dataset in datasets:
        if not dataset.get("source_binding"):
            continue
        dataset["source_relationships"] = []
        for foreign_key in dataset.get("source_binding", {}).get("foreign_keys", []):
            for target in by_oid.get(foreign_key["target_oid"], []):
                positions = {
                    column["position"]: column["name"]
                    for column in target["source_binding"]["columns"]
                }
                if not set(foreign_key["target_positions"]) <= positions.keys():
                    continue
                columns = [positions[position] for position in foreign_key["target_positions"]]
                relation = {
                    "name": foreign_key["name"],
                    "target_id": target["id"],
                    "target_table": target["name"],
                    "source_columns": foreign_key["source_columns"],
                    "target_columns": columns,
                    "validated": foreign_key["validated"],
                    "deferrable": foreign_key["deferrable"],
                }
                dataset["source_relationships"].append(relation)
                if (
                    foreign_key["validated"]
                    and len(columns) == 1
                    and columns == target["primary_key"]
                ):
                    link = {
                        "target_id": target["id"],
                        "source_column": foreign_key["source_columns"][0],
                        "target_column": columns[0],
                        "cardinality": "many_to_one",
                    }
                    if link not in dataset["links"]:
                        dataset["links"].append(link)
    return datasets
