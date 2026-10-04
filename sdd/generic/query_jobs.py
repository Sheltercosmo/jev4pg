"""Durable query ownership, cancellation and result publication."""

from contextlib import contextmanager
import json
import time

from sqlalchemy import and_, insert, or_, select, text, update

from ..ledger import digest, now, uid
from . import schema
from .catalog import serial
from .history import QueryHistory
from .query_requests import QueryInput
from .sql import SQLService


ACTIVE = ("RUNNING", "CANCELLING")


class QueryJobConflict(ValueError):
    pass


def database_time(connection):
    if connection.dialect.name == "postgresql":
        return float(
            connection.execute(text("SELECT EXTRACT(EPOCH FROM clock_timestamp())")).scalar_one()
        )
    return time.time()


class QueryJobs:
    def __init__(self, db, service=None):
        self.db = db
        self.sql = service or SQLService(db)
        self.history = QueryHistory(db)

    @contextmanager
    def transaction(self, tenant):
        with self.db.transaction(tenant) as connection:
            if connection.dialect.name == "postgresql":
                connection.exec_driver_sql("SET LOCAL statement_timeout = '2000ms'")
                connection.exec_driver_sql("SET LOCAL lock_timeout = '1000ms'")
            yield connection

    def submit(self, tenant, actor, role, request, idempotency_key):
        if not actor or len(actor) > 100 or role not in ("reader", "reviewer"):
            raise ValueError("A valid query actor and role are required")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ValueError("Idempotency key must contain 1 to 128 characters")
        body = QueryInput.model_validate(request).model_dump()
        fingerprint = digest([body, role])
        table = schema.query_jobs
        scope = (
            table.c.tenant == tenant,
            table.c.actor == actor,
            table.c.idempotency_key == idempotency_key,
        )
        with self.transaction(tenant) as conn:
            prior = (
                conn.execute(select(table.c.id, table.c.request_hash).where(*scope))
                .mappings()
                .first()
            )
        if prior:
            if prior["request_hash"] != fingerprint:
                raise QueryJobConflict("Idempotency key belongs to a different request")
            return self.get(tenant, actor, prior["id"])
        _, _, datasets, target = self.sql.prepare(tenant, body["sql"])
        if target and role != "reviewer":
            raise PermissionError("Reviewer role required for mutation previews")
        parent = body["parent_history_id"]
        if parent and self.history.get(tenant, actor, parent)["status"] == "redacted":
            raise ValueError("Cannot continue redacted query history")
        identity, timestamp = uid(), now()
        dataset_ids = [item["id"] for item in datasets]
        with self.transaction(tenant) as conn:
            if conn.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as upsert
            else:
                from sqlalchemy.dialects.sqlite import insert as upsert
            added = conn.execute(
                upsert(table)
                .values(
                    id=identity,
                    tenant=tenant,
                    actor=actor,
                    actor_role=role,
                    idempotency_key=idempotency_key,
                    request_hash=fingerprint,
                    request=body,
                    dataset_ids=dataset_ids,
                    catalog_hash=self.sql.catalog_hash(datasets),
                    state="QUEUED",
                    lease_until=0,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                .on_conflict_do_nothing(index_elements=["tenant", "actor", "idempotency_key"])
            ).rowcount
            if added:
                conn.execute(
                    insert(schema.query_history).values(
                        id=identity,
                        tenant=tenant,
                        actor=actor,
                        input={
                            "mode": "sql",
                            "text": body["sql"],
                            "max_evaluations": body["max_evaluations"],
                        },
                        dataset_ids=dataset_ids,
                        parent_id=parent,
                        status="queued",
                        logical_sql=body["sql"],
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
            else:
                prior = conn.execute(select(table).where(*scope)).mappings().one()
                if prior["request_hash"] != fingerprint:
                    raise QueryJobConflict("Idempotency key belongs to a different request")
                identity = prior["id"]
        return self.get(tenant, actor, identity)

    @staticmethod
    def _history(conn, job, status, **values):
        conn.execute(
            update(schema.query_history)
            .where(
                schema.query_history.c.tenant == job["tenant"],
                schema.query_history.c.actor == job["actor"],
                schema.query_history.c.id == job["id"],
                schema.query_history.c.status != "redacted",
            )
            .values(status=status, updated_at=now(), **values)
        )

    def _expire(self, conn, tenant, identity=None):
        table = schema.query_jobs
        query = select(table).where(
            table.c.tenant == tenant,
            table.c.state.in_(ACTIVE),
            table.c.lease_until <= database_time(conn),
        )
        if identity:
            query = query.where(table.c.id == identity)
        rows = (
            conn.execute(
                query.order_by(table.c.lease_until).limit(100).with_for_update(skip_locked=True)
            )
            .mappings()
            .all()
        )
        for job in rows:
            conn.execute(
                update(table)
                .where(
                    table.c.id == job["id"],
                    table.c.tenant == tenant,
                    table.c.state.in_(ACTIVE),
                    table.c.lease_token == job["lease_token"],
                )
                .values(
                    state="FAILED",
                    error="LEASE_EXPIRED",
                    lease_token=None,
                    lease_until=0,
                    outcome=None,
                    updated_at=now(),
                )
            )
            self._history(conn, job, "error", error="LEASE_EXPIRED")

    def claim(self, tenant, lease_seconds=30):
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("Query worker lease must be between 5 and 3600 seconds")
        table = schema.query_jobs
        with self.transaction(tenant) as conn:
            self._expire(conn, tenant)
            job = (
                conn.execute(
                    select(table)
                    .where(table.c.tenant == tenant, table.c.state == "QUEUED")
                    .order_by(table.c.created_at, table.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .first()
            )
            if job is None:
                return None
            values = dict(
                state="RUNNING",
                lease_token=uid(),
                lease_until=database_time(conn) + lease_seconds,
                updated_at=now(),
            )
            changed = conn.execute(
                update(table)
                .where(table.c.tenant == tenant, table.c.id == job["id"], table.c.state == "QUEUED")
                .values(**values)
            ).rowcount
            if not changed:
                return None
            self._history(conn, job, "running")
            return {**job, **values}

    def heartbeat(self, job, lease_seconds):
        table = schema.query_jobs
        with self.transaction(job["tenant"]) as conn:
            current = (
                conn.execute(
                    select(table.c.state, table.c.lease_token, table.c.lease_until)
                    .where(table.c.tenant == job["tenant"], table.c.id == job["id"])
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            clock = database_time(conn)
            if (
                not current
                or current["state"] not in ACTIVE
                or (current["lease_token"] != job["lease_token"] or current["lease_until"] <= clock)
            ):
                return None
            conn.execute(
                update(table)
                .where(table.c.id == job["id"], table.c.tenant == job["tenant"])
                .values(lease_until=clock + lease_seconds, updated_at=now())
            )
            return current["state"]

    def finish(self, job, result=None, *, state="SUCCEEDED", error=None):
        if state not in ("SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"):
            raise ValueError("Invalid query completion state")
        if state == "SUCCEEDED" and not isinstance(result, dict):
            raise ValueError("Successful completion requires a query result")
        outcome = serial(result) if result is not None else None
        if (
            outcome is not None
            and len(json.dumps(outcome, ensure_ascii=False).encode()) > 4 * 1024 * 1024
        ):
            outcome, state, error = None, "FAILED", "RESULT_TOO_LARGE"
        if outcome and outcome.get("run_id"):
            outcome = {key: value for key, value in outcome.items() if key != "result"}
        table = schema.query_jobs
        with self.transaction(job["tenant"]) as conn:
            current = (
                conn.execute(
                    select(table)
                    .where(table.c.tenant == job["tenant"], table.c.id == job["id"])
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if (
                not current
                or current["state"] not in ACTIVE
                or (
                    current["lease_token"] != job["lease_token"]
                    or current["lease_until"] <= database_time(conn)
                )
            ):
                return False
            if current["state"] == "CANCELLING":
                state, outcome, error = "CANCELLED", None, "CANCELLED"
            conn.execute(
                update(table)
                .where(table.c.id == job["id"], table.c.tenant == job["tenant"])
                .values(
                    state=state,
                    outcome=outcome,
                    error=error,
                    lease_until=0,
                    lease_token=None,
                    updated_at=now(),
                )
            )
            if state == "SUCCEEDED":
                status = (
                    "preview"
                    if outcome.get("mutation_preview")
                    else ("complete" if outcome.get("manifest", {}).get("complete") else "partial")
                )
                self._history(
                    conn,
                    current,
                    status,
                    error=None,
                    run_id=outcome.get("run_id"),
                    preview_id=outcome.get("preview_token"),
                )
            else:
                status = {"CANCELLED": "cancelled", "TIMED_OUT": "timed_out"}.get(state, "error")
                self._history(conn, current, status, error=error)
        return True

    def cancel(self, tenant, actor, identity):
        table = schema.query_jobs
        with self.transaction(tenant) as conn:
            self._expire(conn, tenant, identity)
            job = (
                conn.execute(
                    select(table)
                    .where(table.c.tenant == tenant, table.c.actor == actor, table.c.id == identity)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if job is None:
                raise ValueError("Query job is unavailable to this actor")
            if job["state"] in ("QUEUED", "RUNNING"):
                state = "CANCELLED" if job["state"] == "QUEUED" else "CANCELLING"
                conn.execute(
                    update(table)
                    .where(table.c.tenant == tenant, table.c.id == identity)
                    .values(state=state, updated_at=now())
                )
                self._history(conn, job, "cancelled" if state == "CANCELLED" else "cancelling")
        return self.get(tenant, actor, identity)

    def get(self, tenant, actor, identity):
        table = schema.query_jobs
        with self.transaction(tenant) as conn:
            self._expire(conn, tenant, identity)
            job = (
                conn.execute(
                    select(table).where(
                        table.c.tenant == tenant, table.c.actor == actor, table.c.id == identity
                    )
                )
                .mappings()
                .first()
            )
            if job is None:
                raise ValueError("Query job is unavailable to this actor")
            result = dict(job["outcome"]) if job["outcome"] else None
            if result and result.get("run_id"):
                result["result"] = conn.execute(
                    select(schema.runs.c.result).where(
                        schema.runs.c.tenant == tenant, schema.runs.c.id == result["run_id"]
                    )
                ).scalar_one()
        return self._public(job, result)

    @staticmethod
    def _public(job, result=None):
        output, operation = "NOT_EVALUATED", job["state"]
        if job["state"] == "SUCCEEDED":
            manifest = (job["outcome"] or {}).get("manifest", {})
            output = "VALUE" if manifest.get("complete") else "UNKNOWN"
            operation = (
                "TRUNCATED"
                if manifest.get("truncated")
                else (
                    "AWAITING_REVIEW"
                    if (job["outcome"] or {}).get("mutation_preview")
                    else "SUCCEEDED"
                    if output == "VALUE"
                    else "PARTIAL"
                )
            )
        return {
            "id": job["id"],
            "history_id": job["id"],
            "job_state": job["state"],
            "output_state": output,
            "operation_state": operation,
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "error": job["error"],
            "result": result,
        }

    def recent(self, tenant, actor, limit=20, before=None):
        if not 1 <= limit <= 50:
            raise ValueError("Query job page limit must be between 1 and 50")
        table = schema.query_jobs
        query = select(
            table.c.id,
            table.c.state,
            table.c.created_at,
            table.c.updated_at,
            table.c.error,
            table.c.outcome["manifest"]["complete"].as_boolean().label("complete"),
            table.c.outcome["manifest"]["truncated"].as_boolean().label("truncated"),
            table.c.outcome["mutation_preview"].as_boolean().label("preview"),
        ).where(table.c.tenant == tenant, table.c.actor == actor)
        with self.transaction(tenant) as conn:
            self._expire(conn, tenant)
            if before:
                cursor = conn.execute(
                    select(table.c.created_at).where(
                        table.c.tenant == tenant, table.c.actor == actor, table.c.id == before
                    )
                ).scalar_one_or_none()
                if cursor is None:
                    raise ValueError("Query job cursor is unavailable to this actor")
                query = query.where(
                    or_(
                        table.c.created_at < cursor,
                        and_(table.c.created_at == cursor, table.c.id < before),
                    )
                )
            rows = (
                conn.execute(
                    query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(limit + 1)
                )
                .mappings()
                .all()
            )
        return {
            "items": [
                self._public(
                    {
                        **row,
                        "outcome": {
                            "manifest": {
                                "complete": row["complete"],
                                "truncated": row["truncated"],
                            },
                            "mutation_preview": row["preview"],
                        },
                    }
                )
                for row in rows[:limit]
            ],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    @staticmethod
    def redact_dataset(connection, tenant, dataset_id):
        table = schema.query_jobs
        statement = (
            update(table)
            .where(table.c.tenant == tenant)
            .values(
                request={},
                outcome=None,
                state="REDACTED",
                error=None,
                lease_token=None,
                lease_until=0,
                updated_at=now(),
            )
        )
        if connection.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import JSONB

            connection.execute(
                statement.where(table.c.dataset_ids.cast(JSONB).contains([dataset_id]))
            )
        else:
            for row in connection.execute(
                select(table.c.id, table.c.dataset_ids).where(table.c.tenant == tenant)
            ).mappings():
                if dataset_id in row["dataset_ids"]:
                    connection.execute(statement.where(table.c.id == row["id"]))
