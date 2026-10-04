"""Bounded reader for PostgreSQL 17's stored nodeToString representation."""

from dataclasses import dataclass
import re


class NodeLimitError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    tag: str
    fields: dict


@dataclass(frozen=True)
class Datum:
    length: int
    data: tuple[int, ...]


@dataclass(frozen=True)
class TypedList:
    kind: str
    values: tuple[int, ...]


def read_tree(source: str, *, max_chars=1_000_000, max_tokens=100_000, max_depth=96):
    """Decode structure without interpreting constants or calling PostgreSQL functions."""
    if not isinstance(source, str) or not source:
        raise ValueError("Missing PostgreSQL expression tree")
    if len(source) > max_chars:
        raise NodeLimitError("PostgreSQL expression exceeds the character budget")
    tokens = []
    offset = 0
    while offset < len(source):
        if source[offset] in " \t\n":
            offset += 1
            continue
        start = offset
        if source[offset] in "{}()":
            offset += 1
        else:
            while (
                offset < len(source)
                and source[offset] not in " \t\n"
                and source[offset] not in "{}()"
            ):
                if source[offset] == "\\":
                    offset += 1
                    if offset == len(source):
                        raise ValueError("Unterminated PostgreSQL token escape")
                offset += 1
        tokens.append(source[start:offset])
        if len(tokens) > max_tokens:
            raise NodeLimitError("PostgreSQL expression exceeds the token budget")

    cursor = 0

    def take():
        nonlocal cursor
        if cursor == len(tokens):
            raise ValueError("Incomplete PostgreSQL expression tree")
        token = tokens[cursor]
        cursor += 1
        return token

    def peek():
        return tokens[cursor] if cursor < len(tokens) else None

    def value(depth=0):
        if depth > max_depth:
            raise NodeLimitError("PostgreSQL expression exceeds the nesting budget")
        token = take()
        if token == "{":
            tag, fields = take(), {}
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", tag):
                raise ValueError("Invalid PostgreSQL node tag")
            while peek() != "}":
                field = take()
                if not re.fullmatch(r":[A-Za-z][A-Za-z0-9_]*", field) or field[1:] in fields:
                    raise ValueError("Invalid or repeated PostgreSQL node field")
                item = value(depth + 1)
                if tag == "CONST" and field == ":constvalue" and item is not None:
                    if type(item) is not int or item < 0 or take() != "[":
                        raise ValueError("Invalid PostgreSQL constant datum")
                    data = []
                    while peek() != "]":
                        byte = value(depth + 1)
                        if type(byte) is not int or not -128 <= byte <= 255:
                            raise ValueError("Invalid PostgreSQL datum byte")
                        data.append(byte)
                    take()
                    if item > len(data):
                        raise ValueError("Truncated PostgreSQL constant datum")
                    item = Datum(item, tuple(data))
                fields[field[1:]] = item
            take()
            return Node(tag, fields)
        if token == "(":
            kind = take() if peek() in {"i", "o", "b", "x"} else None
            items = []
            while peek() != ")":
                items.append(value(depth + 1))
            take()
            if kind:
                if any(type(item) is not int for item in items):
                    raise ValueError("Invalid PostgreSQL typed list")
                return TypedList(kind, tuple(items))
            return tuple(items)
        if token in {"}", ")"}:
            raise ValueError("Unexpected PostgreSQL tree token")
        if token == "<>":
            return None
        if token in {"true", "false"}:
            return token == "true"
        if re.fullmatch(r"-?\d+", token):
            return int(token)
        decoded = re.sub(r"\\(.)", r"\1", token, flags=re.DOTALL)
        return decoded[1:-1] if token.startswith('"') and token.endswith('"') else decoded

    result = value()
    if cursor != len(tokens):
        raise ValueError("Trailing PostgreSQL expression tokens")
    return result
