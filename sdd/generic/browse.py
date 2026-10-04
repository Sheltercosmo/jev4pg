"""Bounded, live keyset reads for table browsers and application backends."""

from datetime import datetime
from decimal import InvalidOperation
import json
from time import perf_counter
from uuid import UUID

from sqlalchemy import bindparam, select, tuple_
from ..json_response import exact_json

from .catalog import Catalog, coerce, serial
from .source_catalog import source_transaction


PAGE_BYTES = 4 * 1024 * 1024


def typed_value(value, definition):
    if value is None:
        raise ValueError("Use is_null or not_null for NULL filters")
    kind = definition["type"]
    if kind in {"date", "datetime", "text"} and not isinstance(value, str):
        raise ValueError("Text and date filter values must be strings")
    if kind in {"integer", "number"} and (
        isinstance(value, bool) or not isinstance(value, (str, int, float))
    ):
        raise ValueError("Numeric filter values must be finite numbers or decimal strings")
    if definition.get("database_type") == "uuid":
        return UUID(str(value))
    if definition.get("database_type") == "timestamp without time zone":
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is not None:
            raise ValueError("This column requires a timestamp without a timezone")
        return result
    if definition["type"] == "json":
        raise ValueError("JSON columns support null checks; use SQL for JSON predicates")
    try:
        return coerce(value, definition["type"])
    except (InvalidOperation, OverflowError):
        raise ValueError("Invalid numeric filter or cursor value") from None


def page_query(table, dataset, columns, filters, after, limit):
    definitions = {column["name"]: column for column in dataset["columns"]}
    keys = dataset["primary_key"]
    if not keys or any(key not in table.c for key in keys):
        raise ValueError("Table browsing requires an exposed primary key; use a bounded SQL query")
    if keys == ["_sdd_row_id"]:
        definitions["_sdd_row_id"] = {"name": "_sdd_row_id", "type": "text"}
    names = columns if columns is not None else [column["name"] for column in dataset["columns"]]
    if not names or len(set(names)) != len(names) or set(names) - definitions.keys():
        raise ValueError("Choose distinct columns from this dataset")
    projection = list(dict.fromkeys([*names, *keys]))
    query = select(*(table.c[name] for name in projection))
    operations = {
        "eq": "__eq__",
        "ne": "__ne__",
        "gt": "__gt__",
        "gte": "__ge__",
        "lt": "__lt__",
        "lte": "__le__",
    }
    for item in filters:
        name, operation, value = item["column"], item["op"], item.get("value")
        if name not in definitions:
            raise ValueError("Unknown filter column")
        column, definition = table.c[name], definitions[name]
        if operation in {"is_null", "not_null"}:
            if value is not None:
                raise ValueError("Null checks do not accept a value")
            predicate = column.is_(None) if operation == "is_null" else column.is_not(None)
        elif operation == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= 100:
                raise ValueError("An in filter requires 1–100 values")
            predicate = column.in_([typed_value(part, definition) for part in value])
        elif operation == "prefix":
            if definition["type"] != "text" or definition.get("database_type") == "uuid":
                raise ValueError("Prefix filters require a text column")
            predicate = column.startswith(typed_value(value, definition), autoescape=True)
        elif operation in operations:
            if definition["type"] == "boolean" and operation not in {"eq", "ne"}:
                raise ValueError("Boolean filters support equality and null checks")
            predicate = getattr(column, operations[operation])(typed_value(value, definition))
        else:
            raise ValueError("Unsupported filter operation")
        query = query.where(predicate)
    if after is not None:
        if len(after) != len(keys):
            raise ValueError("The page cursor must contain each primary-key value in order")
        values = [
            bindparam(None, typed_value(value, definitions[key]), type_=table.c[key].type)
            for key, value in zip(keys, after)
        ]
        predicate = (
            table.c[keys[0]] > values[0]
            if len(keys) == 1
            else tuple_(*(table.c[key] for key in keys)) > tuple_(*values)
        )
        query = query.where(predicate)
    return query.order_by(*(table.c[key] for key in keys)).limit(limit + 1), names


class TableBrowser:
    def __init__(self, db):
        self.db, self.catalog = db, Catalog(db)

    def scan(self, tenant, identity, *, columns=None, filters=None, after=None, limit=100):
        if not 1 <= limit <= 1000 or len(filters or []) > 16:
            raise ValueError("Use pages of 1–1,000 rows and at most 16 filters")
        started = perf_counter()
        dataset = self.catalog.get(tenant, identity)
        isolation = "REPEATABLE READ" if self.db.engine.dialect.name == "postgresql" else None
        with source_transaction(self.db, tenant, [dataset], isolation) as connection:
            from .sql import SQLService

            SQLService(self.db).configure_transaction(connection)
            # source_transaction has already checked and pinned this relation.
            table = self.catalog.table(dataset, connection, source_validated=True)
            query, names = page_query(table, dataset, columns, filters or [], after, limit)
            rows, last, size, more, boundary = [], None, 0, False, None
            with connection.execution_options(stream_results=True, max_row_buffer=32).execute(
                query
            ) as result:
                for row in result.mappings():
                    if len(rows) == limit:
                        more, boundary = True, "rows"
                        break
                    entry = exact_json(serial({name: row[name] for name in names}))
                    cursor = exact_json(serial([row[key] for key in dataset["primary_key"]]))
                    cost = len(
                        json.dumps([entry, cursor], ensure_ascii=False, allow_nan=False).encode(
                            "utf-8"
                        )
                    )
                    if cost > PAGE_BYTES or len(json.dumps(cursor)) > 65536:
                        raise ValueError(
                            "A row exceeds the page size budget; select fewer or smaller columns with SQL"
                        )
                    if size + cost > PAGE_BYTES:
                        more, boundary = True, "bytes"
                        break
                    rows.append(entry)
                    size += cost
                    last = cursor
        return {
            "dataset_id": dataset["id"],
            "result": rows,
            "columns": names,
            "primary_key": dataset["primary_key"],
            "next_after": last if more else None,
            "has_more": more,
            "page_limited_by": boundary,
            "consistency": "live_keyset",
            "returned_rows": len(rows),
            "execution_ms": round((perf_counter() - started) * 1000, 2),
        }
