"""Paired executor benchmark. Raw reports stay outside tracked source files."""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import platform
from pathlib import Path
import subprocess
import threading
import time
from datetime import datetime, timezone

import httpx

from cases import SPLITS, cases, questions
from pgjev import COMMIT, Reference

ROOT = Path(__file__).resolve().parents[2]
MODEL = "jev-1.13.0"
METHODS = {"native", "native_map", "native_dictionary", "pgjev_request_path", "pgjev_dictionary"}


class Gateway:
    """Identical forwarding and metering for both clients; credentials stay here."""

    def __init__(self, live, key, proxy=None):
        self.live, self.key = live, key
        self.client = httpx.Client(proxy=proxy, timeout=60, limits=httpx.Limits(max_connections=32))
        self.records, self.lock = [], threading.Lock()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                started = time.perf_counter()
                status = 200
                try:
                    if gateway.live:
                        response = gateway.client.post(
                            "https://api.typesafe.ai/v1/systemone",
                            content=raw,
                            headers={
                                "Authorization": "Bearer " + gateway.key,
                                "Content-Type": "application/json",
                            },
                        )
                        status, result = response.status_code, response.json()
                    else:
                        # A latency fixture measures orchestration only, never language accuracy.
                        time.sleep(0.02)
                        result = {
                            "model": body["model"],
                            "answers": {
                                key: {"type": "noul", "noul": 0.95} for key in body["questions"]
                            },
                        }
                except Exception as error:
                    status, result = 502, {"error": type(error).__name__}
                with gateway.lock:
                    gateway.records.append(
                        {
                            "seconds": time.perf_counter() - started,
                            "status": status,
                            "bytes": len(raw),
                            "questions": len(body["questions"]),
                            "usage": result.get("usage", {}),
                        }
                    )
                encoded = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        # Windows' small listen backlog otherwise penalizes the faster client.
        self.server.socket.listen(128)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/systemone"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.client.close()


def score(decisions, labels, conditions):
    count = matched = known = correct_known = failed = 0
    for row, reference in zip(decisions, labels, strict=True):
        for key in conditions:
            count += 1
            answer = row[key]
            p = answer.get("raw", {}).get("noul")
            if p is not None:
                matched += (p >= 0.5) == reference[key]
            if answer["output_state"] == "VALUE":
                known += 1
                correct_known += answer["value"] == reference[key]
            elif answer["output_state"] == "NOT_EVALUATED":
                failed += 1
    return {
        "judgments": count,
        "matches_at_0_5": matched,
        "resolved": known,
        "correct_resolved": correct_known,
        "unknown": count - known - failed,
        "not_evaluated": failed,
    }


def fingerprint():
    paths = sorted((ROOT / "native/core/src").glob("*.rs"))
    return hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in paths)).hexdigest()


def meter(records):
    return {
        "requests": len(records),
        "request_bytes": sum(r["bytes"] for r in records),
        "http_failures": sum(r["status"] != 200 for r in records),
        "input_tokens": sum(r["usage"].get("input_tokens", 0) for r in records),
        "output_tokens": sum(r["usage"].get("output_tokens", 0) for r in records),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=list(SPLITS), required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--proxy")
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument(
        "--workloads", default="single,three_predicates,paraphrased,chinese,renamed,repeated_contexts,wide"
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--methods", default="native,native_map,pgjev_request_path")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--native", type=Path, default=ROOT / ("native/target/release/examples/measure.exe" if os.name == "nt" else "native/target/release/examples/measure")
    )
    parser.add_argument("--reference", type=Path, default=ROOT / ".runtime/pg-jev-reference")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new report path; existing evaluation records are immutable")
    if not set(args.methods.split(",")).issubset(METHODS) or args.repeats < 1:
        parser.error("Supply supported methods and at least one repeat")
    env = dict(os.environ)
    if args.env_file:
        for line in args.env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    api_key = env.get("SDD_JEV_API_KEY") or env.get("TYPESAFE_API_KEY", "")
    if args.live and not api_key:
        parser.error("Set TYPESAFE_API_KEY or pass a private --env-file")
    commit = subprocess.check_output(
        ["git", "-C", str(args.reference), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != COMMIT:
        parser.error("Reference checkout must match the pinned commit")
    gateway = Gateway(args.live, api_key, args.proxy)
    report = {
        "scope": "executor/request path; PostgreSQL scans, LIMIT and durable registry excluded",
        "split": args.split,
        "live": args.live,
        "model": MODEL,
        "upstream_commit": COMMIT,
        "native_source_sha256": fingerprint(),
        "binary_sha256": hashlib.sha256(args.native.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "stream": args.stream,
        "concurrency": 16,
        "pgjev_batch_rows": 20,
        "native_batch_rows": 32,
        "mapped_questions_per_request": 32,
        "mapped_rows_per_window": 512,
        "runs": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [str(args.native.resolve())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        workloads = list(cases(args.split))
        if args.reviews:
            review_source = json.loads(args.reviews.read_text(encoding="utf-8"))
            report["reviews_archive_sha256"] = review_source["archive_sha256"]
            reviews = review_source["splits"][args.split]
            workloads.append(
                (
                    "reviews",
                    [{"review": row["text"]} for row in reviews],
                    [{"positive": row["positive"]} for row in reviews],
                    {
                        "positive": "The review expresses an overall positive opinion about the product, movie or restaurant. Consider negation and contrast."
                    },
                )
            )
        if not set(args.workloads.split(",")).issubset({item[0] for item in workloads}):
            raise ValueError("Unknown workload, or reviews were requested without --reviews")
        for name, rows, labels, conditions in workloads:
            if name not in args.workloads.split(","):
                continue
            arrivals, arrival_labels = [], []
            if args.stream:
                cutoff = len(rows) * 3 // 4
                arrivals = rows[cutoff:] + rows[: len(rows) // 4]
                arrival_labels = labels[cutoff:] + labels[: len(rows) // 4]
                rows, labels = rows[:cutoff], labels[:cutoff]
            for repeat in range(args.repeats):
                methods = args.methods.split(",")
                if repeat % 2:
                    methods.reverse()
                for method in methods:
                    before = len(gateway.records)
                    projections = {}
                    if method in {"native_dictionary", "pgjev_dictionary"}:
                        columns = (
                            {"role": "职业", "country": "国家", "phrase": "备注"}
                            if name == "renamed"
                            else {
                                "role": "occupation",
                                "country": "country",
                                "phrase": "note",
                                "positive": "review",
                            }
                        )
                        projections = {key: [columns[key]] for key in conditions}
                    if method.startswith("native"):
                        request = {
                            "provider": {
                                "endpoint": gateway.endpoint,
                                "model": MODEL,
                                "revision": "pinned",
                            },
                            "limits": {
                                "concurrency": 16,
                                "batch_rows": 32,
                                "max_requests": 10000,
                                "max_judgments": 10000,
                                "max_input_bytes": 128000000,
                            },
                            "rows": rows,
                            "questions": questions(conditions),
                            "warm": True,
                            "arrivals": arrivals,
                        }
                        if method in {"native_map", "native_dictionary"}:
                            request["map"] = True
                            request["projections"] = projections
                        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                        process.stdin.flush()
                        line = process.stdout.readline()
                        if not line:
                            raise RuntimeError(process.stderr.read())
                        passes = json.loads(line)["passes"]
                        boundaries = [p["usage_cumulative"]["requests"] for p in passes]
                    else:
                        reference = Reference(args.reference, gateway.endpoint, MODEL)
                        try:
                            passes, boundaries = [], []
                            for wave in [rows, rows] + ([arrivals] if arrivals else []):
                                passes.append(reference.evaluate(wave, conditions, projections=projections))
                                boundaries.append(len(gateway.records) - before)
                        finally:
                            reference.close()
                    records = gateway.records[before:]
                    if boundaries[-1] != len(records):
                        raise RuntimeError("Request accounting does not match the measured passes")
                    pass_metrics = {}
                    previous = 0
                    for phase, current, measured in zip(["cold", "warm", "arrival"], boundaries, passes):
                        pass_metrics[phase] = {"seconds": measured["seconds"], **meter(records[previous:current])}
                        previous = current
                    run = {
                        "workload": name,
                        "method": method,
                        "repeat": repeat,
                        "rows": len(rows),
                        "cold_seconds": passes[0]["seconds"],
                        "warm_seconds": passes[1]["seconds"],
                        **meter(records[:boundaries[0]]),
                        "passes": pass_metrics,
                        "score": score(passes[0]["decisions"], labels, conditions)
                        if args.live
                        else None,
                        "warm_equal": passes[0]["decisions"] == passes[1]["decisions"],
                        "arrival_seconds": passes[2]["seconds"] if arrivals else None,
                        "arrival_score": score(passes[2]["decisions"], arrival_labels, conditions)
                        if arrivals and args.live
                        else None,
                        "decisions": passes[0]["decisions"],
                        "http_records": records,
                    }
                    report["runs"].append(run)
                    args.output.write_text(
                        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    print(
                        json.dumps(
                            {
                                key: run[key]
                                for key in (
                                    "workload",
                                    "method",
                                    "repeat",
                                    "cold_seconds",
                                    "requests",
                                    "input_tokens",
                                    "score",
                                )
                            }
                        ),
                        flush=True,
                    )
    finally:
        process.stdin.close()
        process.wait(timeout=30)
        gateway.close()


if __name__ == "__main__":
    main()
