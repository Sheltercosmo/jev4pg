"""Request-scoped, bounded value evidence. Samples never define query populations."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import re
from threading import Lock

from sqlalchemy import String, cast, column, func, select, true, values
from sqlalchemy.exc import SQLAlchemyError

from .catalog import serial
from .source_catalog import source_transaction


def mentioned(request, value):
    value = str(value).casefold()
    if not value:
        return False
    if re.search(r"[\u3400-\u9fff]", value):
        return value in request.casefold()
    return bool(
        re.search(
            r"(?<![^\W\u3400-\u9fff])" + re.escape(value) + r"(?![^\W\u3400-\u9fff])",
            request.casefold(),
        )
    )


def literal_terms(request, limit=128):
    """Exact lookup hypotheses from wording, not generated category interpretations."""
    quoted = re.findall(r"[\"“‘「『]([^\"”’」』]{1,160})[\"”’」』]|'([^']{1,160})'", request)
    candidates = [a or b for a, b in quoted]
    words = re.findall(
        r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+(?:[-'][^\W_\u3400-\u9fff]+)*", request, re.UNICODE
    )[:64]
    for width in range(1, 6):
        candidates.extend(" ".join(words[i : i + width]) for i in range(len(words) - width + 1))
    chunks = [
        chunk for chunk in re.findall(r"\w+", request) if re.search(r"[\u3400-\u9fff]", chunk)
    ]
    for width in range(2, 13):
        candidates.extend(
            chunk[i : i + width] for chunk in chunks for i in range(len(chunk) - width + 1)
        )
    result = []
    for candidate in candidates:
        if 1 <= len(candidate) <= 160 and candidate not in result:
            result.append(candidate)
        if len(result) == limit:
            break
    for candidate in tuple(result):
        for variant in (candidate.lower(), candidate.upper(), candidate.title()):
            if variant not in result and len(result) < limit:
                result.append(variant)
    return result


@dataclass
class Sample:
    columns: tuple[str, ...]
    rows: list[dict] = field(default_factory=list)
    clipped: dict[str, int] = field(default_factory=dict)
    complete: bool = False
    output_state: str = "NOT_EVALUATED"
    operation_state: str = "BLOCKED_BY_BUDGET"
    code: str | None = None

    def evidence(self, name, limit=16):
        if name not in self.columns:
            return Sample((name,), code="ColumnBudgetExceeded").evidence(name, limit)
        exact, examples, seen = [], [], set()
        for row in self.rows:
            item = row.get(name)
            if item is None:
                continue
            value = serial(item)
            key = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            if isinstance(value, str) and len(value) > 160:
                examples.append(value[:160])
            else:
                examples.append(value)
                exact.append(value)
        return {
            "examples": examples[:limit],
            "exact_values": exact[:limit],
            "sample_only": True,
            "complete": self.complete and not self.clipped.get(name) and len(exact) <= limit,
            "rows_inspected": len(self.rows),
            "observed_nulls": sum(row.get(name) is None for row in self.rows),
            "observed_blanks": sum(
                isinstance(row.get(name), str) and not row[name].strip() for row in self.rows
            ),
            "text_may_be_truncated": bool(self.clipped.get(name)),
            "output_state": self.output_state,
            "operation_state": self.operation_state,
            **({"code": self.code} if self.code else {}),
        }


class PlanningSamples:
    def __init__(self, catalog, *, row_limit=512, byte_limit=1024 * 1024, read_limit=32):
        self.catalog = catalog
        self.row_limit, self.byte_limit, self.read_limit = row_limit, byte_limit, read_limit
        self.cache, self.probes, self.proofs = {}, {}, {}
        self.lock, self.reads, self.bytes = Lock(), 0, 0

    def _reserve(self):
        with self.lock:
            if self.reads >= self.read_limit or self.bytes >= self.byte_limit:
                return False
            self.reads += 1
            return True

    def sample(self, tenant, dataset, names=None, *, byte_allowance=None):
        available = [c["name"] for c in dataset["columns"] if not c.get("feature_id")]
        names = tuple(dict.fromkeys(names if names is not None else available))
        if set(names) - set(available) - set(dataset["primary_key"]):
            raise ValueError("Unknown planning sample column")
        names = names[:32]
        key = tenant, dataset["id"], names
        with self.lock:
            for (owner, identity, fields), sample in self.cache.items():
                if owner == tenant and identity == dataset["id"] and set(names) <= set(fields):
                    return sample
        result = Sample(names)
        if not names or byte_allowance == 0 or not self._reserve():
            return result
        local_bytes = 0
        try:
            isolation = (
                "REPEATABLE READ" if self.catalog.db.engine.dialect.name == "postgresql" else None
            )
            with source_transaction(self.catalog.db, tenant, [dataset], isolation) as conn:
                if conn.dialect.name == "postgresql":
                    conn.exec_driver_sql("SET LOCAL statement_timeout='750ms'")
                    conn.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
                table = self.catalog.table(dataset, conn, source_validated=True)
                definitions = {c["name"]: c for c in dataset["columns"]}
                fields = []
                for name in names:
                    source = table.c[name]
                    if definitions.get(name, {}).get("type") in {"text", "json"}:
                        source = func.substr(cast(source, String), 1, 161)
                    fields.append(source.label(name))
                query = select(*fields).limit(self.row_limit + 1)
                if dataset["primary_key"]:
                    query = query.order_by(*(table.c[key] for key in dataset["primary_key"]))
                query = query.execution_options(stream_results=True, max_row_buffer=8)
                with conn.execute(query) as rows:
                    result.output_state, result.operation_state = "VALUE", "SUCCEEDED"
                    result.complete = True
                    for row in rows.mappings():
                        entry = dict(row)
                        cost = len(json.dumps(serial(entry), ensure_ascii=False).encode("utf-8"))
                        with self.lock:
                            bounded = (
                                len(result.rows) == self.row_limit
                                or self.bytes + cost > self.byte_limit
                                or byte_allowance is not None
                                and local_bytes + cost > byte_allowance
                            )
                            if not bounded:
                                self.bytes += cost
                                local_bytes += cost
                        if bounded:
                            result.complete, result.operation_state = False, "TRUNCATED"
                            break
                        result.rows.append(entry)
                        for name, value in entry.items():
                            if isinstance(value, str) and len(value) > 160:
                                result.clipped[name] = result.clipped.get(name, 0) + 1
        except (SQLAlchemyError, ValueError) as exc:
            result = Sample(
                names, output_state="UNKNOWN", operation_state="FAILED", code=type(exc).__name__
            )
        with self.lock:
            self.cache[key] = result
        return result

    def many(self, tenant, requests):
        """Read unrelated table batches concurrently; results retain input order."""
        if not requests:
            return []
        with self.lock:
            allowance = max(0, self.byte_limit - self.bytes) // len(requests)
        if self.catalog.db.engine.dialect.name != "postgresql":
            return [self.sample(tenant, *item, byte_allowance=allowance) for item in requests]
        with ThreadPoolExecutor(max_workers=min(4, len(requests))) as pool:
            return list(
                pool.map(
                    lambda item: self.sample(tenant, *item, byte_allowance=allowance), requests
                )
            )

    def column(self, tenant, dataset, name):
        with self.lock:
            for (owner, identity, names), sample in self.cache.items():
                if owner == tenant and identity == dataset["id"] and name in names:
                    return sample
        names = [
            name,
            *[
                c["name"]
                for c in dataset["columns"]
                if c["name"] != name and not c.get("feature_id")
            ],
        ]
        return self.sample(tenant, dataset, names)

    def unique(self, tenant, dataset, name, *, nullable=False):
        key = tenant, dataset["id"], name, nullable
        if key in self.proofs:
            return self.proofs[key]
        self.proofs[key] = None
        if not self.column(tenant, dataset, name).complete or not self._reserve():
            return None
        try:
            with source_transaction(self.catalog.db, tenant, [dataset]) as conn:
                if conn.dialect.name == "postgresql":
                    conn.exec_driver_sql("SET LOCAL statement_timeout='750ms'")
                table = self.catalog.table(dataset, conn, source_validated=True)
                source = select(table.c[name]).limit(self.row_limit + 1).subquery()
                total, present, distinct = conn.execute(
                    select(
                        func.count(),
                        func.count(source.c[name]),
                        func.count(func.distinct(source.c[name])),
                    ).select_from(source)
                ).one()
                if total <= self.row_limit:
                    self.proofs[key] = (
                        bool(present) and present == distinct and (nullable or present == total)
                    )
        except (SQLAlchemyError, ValueError):
            pass
        return self.proofs[key]

    def matches(self, tenant, dataset, name, request):
        key = tenant, dataset["id"], name, request
        if key in self.probes:
            return self.probes[key]
        result = {"values": [], "output_state": "NOT_EVALUATED", "operation_state": "SKIPPED"}
        self.probes[key] = result
        for (owner, identity, names), sample in self.cache.items():
            if owner == tenant and identity == dataset["id"] and name in names and sample.complete:
                result.update(
                    values=[
                        v
                        for v in sample.evidence(name, self.row_limit)["exact_values"]
                        if mentioned(request, v)
                    ],
                    output_state="VALUE",
                    operation_state="SUCCEEDED",
                )
                return result
        terms = literal_terms(request)
        result.update(complete=False, terms_considered=len(terms))
        if not terms or self.catalog.db.engine.dialect.name != "postgresql":
            return result
        if not self._reserve():
            result["operation_state"] = "BLOCKED_BY_BUDGET"
            return result
        try:
            with source_transaction(self.catalog.db, tenant, [dataset], "REPEATABLE READ") as conn:
                conn.exec_driver_sql("SET LOCAL statement_timeout='500ms'")
                conn.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
                table = self.catalog.table(dataset, conn, source_validated=True)
                source = table.c[name]
                if not isinstance(source.type, String):
                    return result
                # One index lookup per literal avoids scanning every occurrence of a common value.
                candidates = values(column("candidate", String), name="_sdd_terms").data(
                    [(v,) for v in terms]
                )
                found = (
                    select(source.label("value"))
                    .where(source == candidates.c.candidate)
                    .limit(1)
                    .lateral("_sdd_match")
                )
                query = select(found.c.value).select_from(candidates.join(found, true()))
                conn.exec_driver_sql("SET LOCAL enable_seqscan=off")
                compiled = query.compile(dialect=conn.dialect)
                plan = conn.exec_driver_sql(
                    "EXPLAIN (FORMAT JSON) " + str(compiled), compiled.params
                ).scalar_one()[0]["Plan"]
                pending, scans = [plan], []
                while pending:
                    node = pending.pop()
                    if node.get("Relation Name") == dataset["table_name"]:
                        scans.append(node)
                    pending.extend(node.get("Plans", []))
                if not scans or any(
                    n["Node Type"] not in {"Index Scan", "Index Only Scan", "Bitmap Heap Scan"}
                    or not (n.get("Index Cond") or n.get("Recheck Cond"))
                    for n in scans
                ):
                    return result
                result.update(output_state="VALUE", operation_state="SUCCEEDED")
                for value in dict.fromkeys(conn.execute(query).scalars()):
                    cost = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                    with self.lock:
                        if self.bytes + cost > self.byte_limit:
                            result["operation_state"] = "TRUNCATED"
                            break
                        self.bytes += cost
                    result["values"].append(value)
        except (SQLAlchemyError, ValueError) as exc:
            result.update(output_state="UNKNOWN", operation_state="FAILED", code=type(exc).__name__)
        self.probes[key] = result
        return result
