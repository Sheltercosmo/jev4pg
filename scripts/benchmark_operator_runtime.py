"""Measure local evidence reads and payload preparation; no model requests."""

import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path

from sqlalchemy import event, insert

from sdd.db import Database
from sdd.operators import schema
from sdd.operators.runtime import Runtime
from sdd.operators.store import Store
from sdd.operators.types import WorkItem


def previous_class(revision, module, name):
    source = subprocess.check_output(["git", "show", f"{revision}:sdd/operators/{module}.py"])
    namespace = {"__package__": "sdd.operators", "__name__": f"benchmark_baseline_{module}"}
    exec(compile(source, f"{revision}/{module}.py", "exec"), namespace)
    return namespace[name]


def measure(store_type, runtime_type, db, keys, items):
    store = store_type(db, "benchmark", "local")
    statements = 0

    def count(_, __, statement, *args):
        nonlocal statements
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "jev_operator_observations" in statement
        ):
            statements += 1

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        start = time.perf_counter()
        if hasattr(store, "cached_many"):
            evidence = store.cached_many(keys)
        else:
            evidence = {key: store.cached(key) for key in keys}
        lookup_ms = (time.perf_counter() - start) * 1000
        assert len(evidence) == len(keys) and all(row is not None for row in evidence.values())
    finally:
        event.remove(db.engine, "before_cursor_execute", count)
    runtime = runtime_type(store, None, "benchmark")
    aliases = {runtime.key(item): [item.id] for item in items}
    start = time.perf_counter()
    batches = runtime._pack_batches(items, aliases, {})
    packing_ms = (time.perf_counter() - start) * 1000
    assert len(batches) == 32 and sum(map(len, batches)) == len(items)
    return {
        "lookup_ms": lookup_ms,
        "lookup_selects": statements,
        "packing_ms": packing_ms,
        "batches": [[item.id for item in batch] for batch in batches],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", help="Trusted local Git revision to compare")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    methods = {"current": (Store, Runtime)}
    baseline = None
    if args.baseline_ref:
        baseline = subprocess.check_output(
            ["git", "rev-parse", "--verify", "--end-of-options", args.baseline_ref + "^{commit}"],
            text=True,
        ).strip()
        methods = {
            "baseline": (
                previous_class(baseline, "store", "Store"),
                previous_class(baseline, "runtime", "Runtime"),
            ),
            **methods,
        }
    db = Database("sqlite:///:memory:")
    db.initialize()
    keys = [f"key{i}" for i in range(600)]
    with db.transaction("benchmark") as connection:
        connection.execute(
            insert(schema.observations),
            [
                dict(
                    id=key,
                    tenant="benchmark",
                    cache_key=key,
                    model="fixture",
                    subject_id=key,
                    source_revisions=["v1"],
                    question={"type": "noul", "instructions": "完成了吗？"},
                    context_hash=key,
                    hypothesis={},
                    answer={"type": "noul", "noul": 0.7},
                    created_at="2026-01-01",
                )
                for key in keys
            ],
        )
    items = [
        WorkItem(
            str(i),
            {"说明": "中文内容" * 300, "n": 9007199254740993},
            {"type": "noul", "instructions": f"检查条件 {i}"},
            "shared",
            ("v1",),
        )
        for i in range(1024)
    ]
    samples, reference = [], None
    try:
        for repeat in range(7):
            order = list(methods) if repeat % 2 == 0 else list(reversed(methods))
            for name in order:
                result = measure(*methods[name], db, keys, items)
                batches = result.pop("batches")
                if reference is None:
                    reference = batches
                assert reference == batches
                samples.append({"method": name, "repeat": repeat, **result})
    finally:
        db.engine.dispose()
    report = {
        "baseline": baseline,
        "lookup_keys": len(keys),
        "packing_questions": len(items),
        "samples": samples,
        "median": {
            name: {
                key: statistics.median(s[key] for s in samples if s["method"] == name)
                for key in ("lookup_ms", "lookup_selects", "packing_ms")
            }
            for name in methods
        },
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(json.dumps(report["median"], indent=2))


if __name__ == "__main__":
    main()
