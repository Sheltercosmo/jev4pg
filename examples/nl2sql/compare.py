"""Run the public tutorial comparison without training or altering the planner."""

# ruff: noqa: E402

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from itertools import permutations
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sdd.config import load_env
from sdd.db import Database
from sdd.generic.catalog import Catalog, serial
from sdd.generic.jev import Decisions
from sdd.generic.llm import StructuredLLM
from sdd.generic.planner import Planner
from sdd.generic.planning_review import PlanReviewRequired
from sdd.generic.sql import SQLService
from sdd.providers import configured_backend

ANSWER = {
    "type": "object",
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
    "additionalProperties": False,
}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def source_hash():
    return digest(
        b"".join(
            str(p.relative_to(ROOT)).encode() + b"\0" + p.read_bytes().replace(b"\r\n", b"\n")
            for p in sorted((ROOT / "sdd").rglob("*.py"))
        )
    )


def matches(actual, expected, ordered):
    if len(actual) != len(expected) or not expected:
        return False
    width = len(expected[0])
    if any(len(row) != width for row in actual):
        return False

    def atom(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError("Nonfinite result")
            return round(value, 6)
        return value

    target = [tuple(map(atom, row)) for row in expected]
    for order in permutations(range(width)):
        rows = [tuple(atom(row[i]) for i in order) for row in actual]
        if (rows == target) if ordered else (Counter(rows) == Counter(target)):
            return True
    return False


class MeteredDecisions(Decisions):
    def __init__(self, *args):
        super().__init__(*args)
        self.requests = self.judgments = 0
        self.usage = Counter()
        self.lock = Lock()

    def ask(self, tenant, state, questions):
        with self.lock:
            self.requests += 1
            self.judgments += len(questions)
        result = super().ask(tenant, state, questions)
        with self.lock:
            self.usage.update(result.get("usage", {}))
        return result


class MeteredLLM(StructuredLLM):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def generate(self, prompt, schema):
        value, record = super().generate(prompt, schema)
        self.records.append(record)
        return value, record


def setup(path, dataset):
    db = Database("sqlite:///" + str(path))
    db.initialize()
    Catalog(db).create("tutorial", **dataset)
    return db


def run_case(case, dataset, method, args):
    with tempfile.TemporaryDirectory(prefix="jevsd-example-") as directory:
        db = setup(Path(directory) / "data.sqlite", dataset)
        decisions = None
        llm = MeteredLLM(args.model, transport=args.transport, effort=args.effort)
        if method != "llm":
            decisions = MeteredDecisions(
                db, configured_backend(), os.getenv("SDD_JEV_MODEL", "jev-1.13.0")
            )
        service = SQLService(db, decisions)
        record = {
            "id": case["id"],
            "method": method,
            "variant_of": case["variant_of"],
            "question": case["question"],
            "status": "FAILED",
            "match": False,
            "held": False,
            "sql": None,
        }
        start = time.perf_counter()
        try:
            if method == "llm":
                catalog = Catalog(db).model_catalog("tutorial", question=case["question"])
                catalog = [
                    {key: table[key] for key in ("name", "description", "columns", "primary_key")}
                    for table in catalog
                ]
                prompt = (
                    "Write one read-only SQL query for this request using the provided catalog. "
                    "Return only the requested columns, preserve duplicates unless distinct is requested, "
                    "and honor requested ordering and NULL semantics. Use standard SQL compatible with SQLite. "
                    "Do not call tools or return literal answers. No execution feedback is available.\n"
                    + json.dumps(
                        {"question": case["question"], "catalog": serial(catalog)},
                        ensure_ascii=False,
                    )
                )
                value, _ = llm.generate(prompt, ANSWER)
                sql = value["sql"]
            else:
                try:
                    plan = Planner(
                        db, decisions, "hybrid" if method == "hybrid" else "staged", llm
                    ).plan("tutorial", case["question"])
                except PlanReviewRequired as exc:
                    plan, record["held"] = exc.plan, True
                sql = plan.get("logical_sql")
            record["planning_ms"] = round((time.perf_counter() - start) * 1000, 2)
            record["sql"] = sql
            if sql:
                output = service.execute("tutorial", sql)
                manifest = output["manifest"]
                record["rows"] = output["result"]
                complete = manifest["complete"] and not manifest["truncated"]
                record["status"] = "COMPLETED" if complete else "INCOMPLETE"
                record["match"] = complete and matches(
                    [list(row.values()) for row in output["result"]],
                    case["expected"],
                    case["ordered"],
                )
            else:
                record["status"] = "NO_PROPOSAL"
        except Exception as exc:
            record["error"] = type(exc).__name__
        record["total_ms"] = round((time.perf_counter() - start) * 1000, 2)
        record["llm"] = llm.records
        record["jev"] = (
            {
                "requests": decisions.requests,
                "judgments": decisions.judgments,
                "usage": dict(decisions.usage),
            }
            if decisions
            else None
        )
        db.engine.dispose()
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/tutorial-comparison.json")
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--transport", default="codex_cli", choices=["codex_cli", "openai"])
    parser.add_argument("--effort", default="low")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    case_path = Path(__file__).with_name("cases.json")
    packet = json.loads(case_path.read_text(encoding="utf-8"))
    datasets = {item["name"]: item for item in packet["datasets"]}
    for case in packet["cases"]:
        with tempfile.TemporaryDirectory() as directory:
            db = setup(Path(directory) / "data.sqlite", datasets[case["dataset"]])
            rows = SQLService(db).execute("tutorial", case["reference_sql"])["result"]
            assert matches(
                [list(row.values()) for row in rows], case["expected"], case["ordered"]
            ), case["id"]
            db.engine.dispose()
    print("All 11 references match independently specified expected rows.", flush=True)
    if args.validate_only:
        return
    if args.output.exists():
        raise ValueError("Choose a new output path; existing runs are immutable")
    frozen = source_hash()
    report = {
        "version": "0.5.0",
        "release_commit": "ecea7d8a49ba05541776f1d6fc674b4395e15dae",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": frozen,
        "cases_sha256": digest(case_path.read_bytes()),
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "llm_model": args.model,
        "effort": args.effort,
        "llm_transport": args.transport,
        "jev_model": os.getenv("SDD_JEV_MODEL", "jev-1.13.0"),
        "database": "isolated SQLite per method and case",
        "records": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=2) as pool:
        for index, case in enumerate(packet["cases"]):
            methods = ["jev", "llm", "hybrid"]
            methods = methods[index % 3 :] + methods[: index % 3]
            jobs = [
                pool.submit(run_case, case, datasets[case["dataset"]], method, args)
                for method in methods
            ]
            for job in jobs:
                result = job.result()
                report["records"].append(result)
                args.output.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(
                    result["id"],
                    result["method"],
                    result["match"],
                    result["status"],
                    result["total_ms"],
                    flush=True,
                )
    assert source_hash() == frozen, "Production code changed during measurement"
    report["summary"] = {}
    for method in ["jev", "llm", "hybrid"]:
        records = [row for row in report["records"] if row["method"] == method]
        report["summary"][method] = {
            "matches": sum(row["match"] for row in records),
            "attempts": len(records),
            "held": sum(row["held"] for row in records),
            "median_ms": statistics.median(row["total_ms"] for row in records),
            "objective_matches": sum(row["match"] for row in records if not row["variant_of"]),
            "variant_matches": sum(row["match"] for row in records if row["variant_of"]),
        }
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
