"""Bounded PostgreSQL previews and atomic commits over reviewed target rows."""

import json
import time

from sqlalchemy import insert, select, text, update
from sqlglot import exp

from ..ledger import digest, now, uid
from . import schema
from .catalog import serial
from .source_catalog import qualified, source_transaction


REVIEW_BYTES = 4 * 1024 * 1024


def _pending(connection, tenant, token, actor):
    preview = (
        connection.execute(
            select(schema.previews)
            .where(schema.previews.c.id == token, schema.previews.c.tenant == tenant)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if (
        not preview
        or preview["state"] != "pending"
        or preview["expires_at"] < time.time()
        or preview["actor"] != actor
    ):
        raise ValueError("Preview is invalid, expired, used, or belongs to another reviewer")
    return preview


def _contract(service, connection, target):
    quoted = qualified(connection, target["schema_name"], target["table_name"])
    connection.exec_driver_sql(f"LOCK TABLE {quoted} IN ACCESS SHARE MODE")
    table = service.catalog.table(target, connection, source_validated=True)
    oid = connection.execute(
        text("SELECT CAST(CAST(:relation AS regclass) AS oid)::bigint"), {"relation": quoted}
    ).scalar_one()
    if list(table.primary_key.columns.keys()) != target["primary_key"]:
        raise ValueError("Target primary key changed; review its catalog definition")
    return table, {
        "dataset": target,
        "oid": oid,
        "columns": [
            [c.name, c.type.compile(dialect=connection.dialect), c.nullable] for c in table.columns
        ],
    }


def _targets(service, connection, tree, target, table, limit, *, lock):
    """Read before images and typed assignments in one bounded database query."""
    node = tree.this.copy()
    projections, assignments = [exp.Star()], []
    if isinstance(tree, exp.Update):
        for index, assignment in enumerate(tree.expressions):
            name, alias = assignment.this.name, f"_sdd_change_{index}"
            kind = exp.DataType.build(table.c[name].type.compile(dialect=connection.dialect))
            projections.append(
                exp.alias_(exp.Cast(this=assignment.expression.copy(), to=kind), alias, quoted=True)
            )
            assignments.append((name, alias))
    query = exp.select(*projections).from_(node)
    if tree.args.get("where"):
        query.set("where", tree.args["where"].copy())
    query = query.order_by(
        *[exp.column(key, table=node.alias_or_name, quoted=True) for key in target["primary_key"]]
    ).limit(limit + 1)
    sql, params = service.bind(query, {id(node): target})
    if lock:
        sql += " FOR UPDATE"
    before, changes, size = [], [], 0
    # Statement-local streaming avoids putting later DML on a server cursor.
    statement = text(sql).execution_options(stream_results=True, max_row_buffer=1)
    with connection.execute(statement, params) as result:
        for row in result.mappings():
            if len(before) == limit:
                raise ValueError(f"Mutation exceeds the {limit}-row budget; narrow its predicate")
            values = dict(row)
            changed = {name: values.pop(alias) for name, alias in assignments}
            size += len(json.dumps(serial([values, changed]), ensure_ascii=False).encode("utf-8"))
            if size > REVIEW_BYTES:
                raise ValueError("Mutation review exceeds 4 MiB; use a smaller batch")
            before.append(values)
            changes.append(changed)
    return before, changes


def _pin(tree, target, before):
    from .sql import literal

    pinned = tree.copy()
    qualifier = tree.this.alias_or_name
    keys = target["primary_key"]
    columns = [exp.column(key, table=qualifier, quoted=True) for key in keys]
    subject = columns[0] if len(keys) == 1 else exp.Tuple(expressions=columns)
    values = []
    for row in before:
        parts = [literal(row[key]) for key in keys]
        values.append(parts[0] if len(keys) == 1 else exp.Tuple(expressions=parts))
    predicate = exp.In(this=subject, expressions=values) if values else exp.false()
    pinned.set("where", exp.Where(this=predicate))
    pinned.set("returning", exp.Returning(expressions=[exp.Star()]))
    return pinned


def execute_mutation(
    service,
    tenant,
    sql,
    tree,
    bindings,
    target,
    *,
    request,
    plan,
    allow_all,
    max_affected,
    actor,
    mutation_token,
    started,
):
    if isinstance(tree, (exp.Update, exp.Delete)) and not tree.args.get("where") and not allow_all:
        raise ValueError("A full-table mutation requires allow_all=true and an explicit preview")
    compiled, params = service.bind(tree, bindings)
    with source_transaction(service.db, tenant, [target], "REPEATABLE READ") as connection:
        service.configure_transaction(connection)
        preview = _pending(connection, tenant, mutation_token, actor) if mutation_token else None
        table, contract = _contract(service, connection, target)
        before, changes = [], []
        if isinstance(tree, exp.Insert):
            affected = len(tree.expression.expressions)
            sample = [
                {
                    column.name: value.sql(dialect="postgres")
                    for column, value in zip(tree.this.expressions, row.expressions)
                }
                for row in tree.expression.expressions[:20]
            ]
            if affected > max_affected:
                raise ValueError(f"Mutation exceeds the {max_affected}-row budget")
        else:
            before, changes = _targets(
                service, connection, tree, target, table, max_affected, lock=bool(preview)
            )
            affected, sample = (
                len(before),
                serial(changes[:20]) if isinstance(tree, exp.Update) else [],
            )
        fingerprint = digest(serial([contract, sql, before, changes]))
        manifest = {
            "execution_backend": "postgresql",
            "operation": type(tree).__name__.lower(),
            "dataset_ids": [target["id"]],
            "source_snapshot": fingerprint,
            "snapshot_mode": "reviewed_targets",
            "mutation_scope": "reviewed_targets",
            "source_rows": None,
            "source_rows_state": "NOT_EVALUATED",
            "reviewed_rows": affected,
            "complete": True,
            "semantic_coverage": {},
            "evidence": [],
            "result_is_partial": False,
            "planning_ms": (plan or {}).get("planning_ms", 0),
        }
        if not preview:
            token = uid()
            connection.execute(
                insert(schema.previews).values(
                    id=token,
                    tenant=tenant,
                    logical_sql=sql,
                    dataset_ids=[target["id"]],
                    snapshot_hash=fingerprint,
                    affected_rows=affected,
                    options={"allow_all": allow_all, "max_affected": max_affected},
                    expires_at=time.time() + 600,
                    state="pending",
                    actor=actor,
                    created_at=now(),
                )
            )
            manifest["execution_ms"] = round((time.perf_counter() - started) * 1000, 2)
            return {
                "mutation_preview": True,
                "preview_token": token,
                "expires_in_seconds": 600,
                "logical_sql": sql,
                "compiled_sql": compiled,
                "parameters": serial(params),
                "affected_rows": affected,
                "before_sample": serial(before[:20]),
                "changes_sample": sample,
                "manifest": manifest,
                "plan": plan or {},
            }
        if (
            preview["logical_sql"] != sql
            or preview["snapshot_hash"] != fingerprint
            or preview["affected_rows"] != affected
        ):
            raise ValueError("Mutation preview is stale; inspect a new preview")
        if not isinstance(tree, exp.Insert):
            compiled, params = service.bind(_pin(tree, target, before), bindings)
            returned = [dict(row) for row in connection.execute(text(compiled), params).mappings()]
            expected = [dict(row, **change) for row, change in zip(before, changes)]
            if isinstance(tree, exp.Delete):
                expected = before
            if sorted(digest(serial(row)) for row in returned) != sorted(
                digest(serial(row)) for row in expected
            ):
                raise ValueError(
                    "Database write differs from the reviewed result; transaction rolled back"
                )
            changed = len(returned)
        else:
            changed = connection.execute(text(compiled), params).rowcount
            if changed != affected:
                raise ValueError(
                    "Database write differs from the reviewed row count; transaction rolled back"
                )
        connection.execute(
            update(schema.previews)
            .where(schema.previews.c.id == mutation_token, schema.previews.c.tenant == tenant)
            .values(state="committed")
        )
        service.after_mutation(connection, tenant, target, tree, before)
        manifest.update(
            committed=True,
            affected_rows=changed,
            execution_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return service.save(
            connection,
            tenant,
            request,
            sql,
            compiled,
            params,
            plan,
            manifest,
            [{"affected_rows": changed}],
        )
