"""Bound result transfer before driver decoding and retain an exact bounded prefix."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json

from sqlalchemy import BigInteger, Text, case, cast, column, func, literal, select, text, true

from ..json_response import exact_json
from ..query_control import checkpoint
from .catalog import serial


RESULT_BYTES = 4 * 1024 * 1024
RESULT_ROWS = 1000
FETCH_ROWS = 128
FETCH_BYTES = 64 * 1024


def encoded_size(value):
    return len(
        json.dumps(
            exact_json(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    )


class OversizedResultRow(ValueError):
    def __init__(self):
        super().__init__(
            "A result row exceeds the 4 MiB budget; select fewer or smaller columns, "
            "or export through PostgreSQL"
        )


@contextmanager
def result_cursor(connection, query, parameters=None, *, byte_limit=RESULT_BYTES):
    """Preserve original value types; the internal marker never represents source NULLs."""
    names = list(query.selected_columns.keys())
    if len(names) != len(set(names)):
        raise ValueError("Duplicate output names require distinct SQL aliases")
    marker = "_sdd_result_bytes"
    while marker in names:
        marker += "_"
    if connection.dialect.name == "postgresql":
        source = query.subquery("_sdd_result")
        overhead = sum(len(name.encode("utf-8")) + 8 for name in names) + 2
        size = literal(overhead, type_=BigInteger())
        for name in names:
            size += cast(
                func.coalesce(
                    func.pg_catalog.octet_length(
                        func.pg_catalog.convert_to(cast(source.c[name], Text()), "UTF8")
                    ),
                    4,
                ),
                BigInteger(),
            )
        # OFFSET keeps the lateral size calculation separate from the value projection.
        sizes = select(size.label("bytes")).correlate(source).offset(0).lateral("_sdd_size")
        measured = (
            select(*source.c, sizes.c.bytes.label(marker))
            .select_from(source.join(sizes, true()))
            .subquery("_sdd_measured")
        )
        cumulative = func.sum(measured.c[marker]).over(rows=(None, 0))
        query = select(
            *(
                case((cumulative <= byte_limit, measured.c[name]), else_=None).label(name)
                for name in names
            ),
            cumulative.label(marker),
        ).select_from(measured)
    else:
        marker = None
    statement = query.execution_options(stream_results=True, max_row_buffer=FETCH_ROWS)
    with connection.execute(statement, parameters or {}) as cursor:
        yield cursor, names, marker


def result_row(row, marker, *, byte_limit=RESULT_BYTES):
    if marker and row[marker] > byte_limit:
        raise OversizedResultRow()
    return {name: value for name, value in row.items() if name != marker}


def result_mappings(cursor, marker):
    """Size batches from the first row; the database prefix gate enforces the byte bound."""
    for index, row in enumerate(cursor.mappings()):
        if index == 0 and marker:
            cursor.yield_per(min(FETCH_ROWS, max(1, FETCH_BYTES // max(1, int(row[marker])))))
        yield row


@dataclass
class ResultWindow:
    columns: list
    rows: list = field(default_factory=list)
    size: int = 2
    limited_by: str | None = None

    def append(self, value, *, row_limit=RESULT_ROWS, byte_limit=RESULT_BYTES):
        checkpoint()
        if len(self.rows) == row_limit:
            self.limited_by = "rows"
            return False
        cost = encoded_size(value) + bool(self.rows)
        if self.size + cost > byte_limit:
            if not self.rows:
                raise OversizedResultRow()
            self.limited_by = "bytes"
            return False
        self.rows.append(value)
        self.size += cost
        return True

    def manifest(self):
        return {
            "result_columns": self.columns,
            "result_bytes": self.size,
            "result_limit_bytes": RESULT_BYTES,
            "result_limit_rows": RESULT_ROWS,
            "result_limited_by": self.limited_by,
            "truncated": self.limited_by is not None,
        }


def read_result(connection, sql, parameters):
    with connection.execute(
        text("SELECT * FROM (" + sql + ") AS _sdd_columns LIMIT 0"), parameters
    ) as description:
        names = list(description.keys())
    if len(names) != len(set(names)):
        raise ValueError("Duplicate output names require distinct SQL aliases")
    query = select(
        *text(sql).columns(*(column(name) for name in names)).subquery("_sdd_query").c
    ).limit(RESULT_ROWS + 1)
    window = ResultWindow(names)
    with result_cursor(connection, query, parameters) as (cursor, _, marker):
        for raw in result_mappings(cursor, marker):
            checkpoint()
            if len(window.rows) == RESULT_ROWS:
                window.limited_by = "rows"
                break
            try:
                row = serial(result_row(raw, marker))
            except OversizedResultRow:
                if not window.rows:
                    raise
                window.limited_by = "bytes"
                break
            if not window.append(row):
                break
    return window
