"""Reviewed CSV imports with conservative types and exact numeric values."""

import csv
from datetime import date, datetime
from decimal import Context, Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
import json
import re

from .catalog import coerce, serial
from ..json_response import exact_json

DECIMAL_CONTEXT = Context(prec=48)


def convert(value, kind, null_empty):
    if value == "" and null_empty:
        return None
    if kind == "text":
        return value
    if kind == "boolean":
        if value.lower() not in {"true", "false"}:
            raise ValueError("Expected true or false")
        return value.lower() == "true"
    if kind == "number":
        number = Decimal(value)
        if not number.is_finite() or number.copy_abs() >= Decimal("1e28"):
            raise ValueError("Number exceeds numeric(38,10)")
        if number != number.quantize(Decimal("1e-10"), context=DECIMAL_CONTEXT):
            raise ValueError("Number has more than 10 decimal places")
        return number
    if kind == "json":
        return coerce(json.loads(value), "json")
    return coerce(value, kind)


def inferred_type(values):
    if not values:
        return "text"
    if all(value.lower() in {"true", "false"} for value in values):
        return "boolean"
    if all(re.fullmatch(r"-?(0|[1-9]\d*)", value) for value in values):
        if all(-(2**63) <= int(value) < 2**63 for value in values):
            return "integer"
    if all(re.fullmatch(r"-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?", value) for value in values):
        try:
            for value in values:
                convert(value, "number", False)
            return "number"
        except (ValueError, InvalidOperation):
            return "text"
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in values):
        try:
            for value in values:
                date.fromisoformat(value)
            return "date"
        except ValueError:
            pass
    if all(re.match(r"\d{4}-\d{2}-\d{2}T", value) for value in values):
        try:
            if all(datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo for value in values):
                return "datetime"
        except ValueError:
            pass
    return "text"


def preview_csv(name, content, *, delimiter="auto", null_empty=True, columns=None):
    if len(content.encode("utf-8")) > 5_000_000 or "\x00" in content:
        raise ValueError("CSV must be UTF-8 text, at most 5 MB, without NUL characters")
    content = content.removeprefix("\ufeff")
    if delimiter == "auto":
        try:
            delimiter = csv.Sniffer().sniff(content[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    if delimiter not in {",", ";", "\t", "|"}:
        raise ValueError("Choose comma, semicolon, tab or pipe as the CSV delimiter")
    reader = csv.reader(StringIO(content, newline=""), delimiter=delimiter, strict=True)
    try:
        header = next(reader, [])
        if not 1 <= len(header) <= 64 or any(
            not value.strip() or len(value.encode("utf-8")) > 63 for value in header
        ):
            raise ValueError(
                "CSV requires 1–64 named columns, with names of at most 63 UTF-8 bytes"
            )
        if len(set(header)) != len(header) or any(
            value.startswith(("_sdd", "__jev_")) for value in header
        ):
            raise ValueError("CSV column names must be unique and must not use reserved prefixes")
        records = []
        for record in reader:
            if not record:
                continue
            if len(record) != len(header):
                raise ValueError(
                    f"CSV line {reader.line_num}: expected {len(header)} cells, found {len(record)}"
                )
            records.append(record)
            if len(records) > 10000:
                raise ValueError("Workspace CSV imports support at most 10,000 rows")
    except csv.Error as exc:
        raise ValueError(f"CSV line {reader.line_num}: {exc}") from None
    if columns is None:
        columns = [
            {
                "name": column,
                "type": inferred_type(
                    [row[index] for row in records if not (null_empty and row[index] == "")]
                ),
                "nullable": True,
            }
            for index, column in enumerate(header)
        ]
    if [column["name"] for column in columns] != header:
        raise ValueError("Reviewed columns must match the CSV header in order")
    columns = [
        {"description": "", "aliases": [], "unit": "", "nullable": True, **column}
        for column in columns
    ]
    rows, errors = [], []
    for number, record in enumerate(records, 1):
        row = {}
        for definition, value in zip(columns, record):
            try:
                converted = convert(value, definition["type"], null_empty)
                if converted is None and not definition.get("nullable", True):
                    raise ValueError("A value is required")
                row[definition["name"]] = converted
            except (ValueError, InvalidOperation, TypeError) as exc:
                row[definition["name"]] = value
                if len(errors) < 20:
                    errors.append({"row": number, "column": definition["name"], "reason": str(exc)})
        rows.append(row)
    fingerprint = sha256(
        json.dumps(
            [name, content, delimiter, null_empty, columns],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "name": name,
        "columns": columns,
        "delimiter": delimiter,
        "row_count": len(rows),
        "sample": exact_json(serial(rows[:20])),
        "errors": errors,
        "valid": not errors,
        "fingerprint": fingerprint,
    }, rows
