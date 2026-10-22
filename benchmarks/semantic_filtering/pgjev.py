"""Load the pinned upstream request builder and HTTP client without altering them.

This measures its request path, not PostgreSQL's tuple/read-ahead executor.
"""

import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import random
import select
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

COMMIT = "e3450d44f7f6745aa2d728c9289b9ca4a6feda9a"
FUNCTIONS = {
    "build_question",
    "request_body",
    "conn_key",
    "alive",
    "borrow_conn",
    "release_conn",
    "tcp_keepalive",
    "retry_after_seconds",
    "call_api",
}


class Reference:
    def __init__(self, checkout, endpoint, model, concurrency=16):
        path = Path(checkout) / "sql/jev--0.2.1.sql"
        source = (
            path.read_text(encoding="utf-8")
            .split("AS $py$", 1)[1]
            .split("# ---------------------------------------------------------------- main", 1)[0]
        )
        tree = ast.parse(source)
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
        ]
        if {node.name for node in functions} != FUNCTIONS:
            raise ValueError("Upstream request path changed")
        self.namespace = {
            "http": http,
            "json": json,
            "ssl": ssl,
            "socket": socket,
            "select": select,
            "random": random,
            "time": time,
            "LOCK": threading.Lock(),
            "S": {"conns": {}, "stats": {"retries": 0}},
            "kind": "noul",
            "opts": None,
        }
        self.code = compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec")
        exec(self.code, self.namespace)
        url = urlsplit(endpoint)
        self.config = {
            "model": model,
            "scheme": url.scheme,
            "host": url.hostname,
            "port": url.port,
            "path": url.path,
            "api_key": None,
            "timeout": 45,
            "keepalive": 600,
            "concurrency": concurrency,
        }
        self.cache = {}
        self.concurrency = concurrency
        self.source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    def evaluate(self, rows, conditions, batch_size=20, projections=None):
        started = time.perf_counter()
        results = [dict() for _ in rows]
        tasks = []

        def context(row, name):
            return (
                {column: row[column] for column in projections[name]}
                if projections and name in projections
                else row
            )

        for name, condition in conditions.items():
            namespace = {**self.namespace, "query": condition}
            exec(self.code, namespace)
            # Both runners reuse identical projected contexts, including in-flight duplicates.
            missing = {}
            for original in rows:
                row = context(original, name)
                key = (condition, json.dumps(row, ensure_ascii=False, sort_keys=True))
                if key not in self.cache:
                    missing[key] = row
            entries = list(missing.items())
            tasks.extend(
                (namespace, entries[i : i + batch_size]) for i in range(0, len(entries), batch_size)
            )

        def call(task):
            namespace, batch = task
            response = namespace["call_api"](self.config, [row for _, row in batch])
            answers = response["answers"]
            return [(key, answers[f"r{i}"]) for i, (key, _) in enumerate(batch)]

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for answered in pool.map(call, tasks):
                self.cache.update(answered)
        for name, condition in conditions.items():
            for i, row in enumerate(rows):
                row = context(row, name)
                key = (condition, json.dumps(row, ensure_ascii=False, sort_keys=True))
                p = self.cache[key]["noul"]
                value = p >= 0.8 if p >= 0.8 or p <= 0.2 else None
                results[i][name] = {
                    "output_state": "UNKNOWN" if value is None else "VALUE",
                    "value": value,
                    "raw": self.cache[key],
                    "operation_state": "SUCCEEDED",
                }
        return {"seconds": time.perf_counter() - started, "decisions": results}

    def close(self):
        for pool in self.namespace["S"]["conns"].values():
            for connection, _ in pool:
                connection.close()
