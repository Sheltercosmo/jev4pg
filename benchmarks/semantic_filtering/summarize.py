"""Export aggregate measurements without source rows or provider responses."""

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
from statistics import mean, median


def summarize(report):
    groups = defaultdict(list)
    for run in report["runs"]:
        groups[run["workload"], run["method"]].append(run)
    for (workload, method), runs in groups.items():
        for phase in ["cold"] + (["arrival"] if report["stream"] else []):
            scores = [r["score" if phase == "cold" else "arrival_score"] for r in runs]
            judgments = sum(s["judgments"] for s in scores)
            measurements = [r["passes"][phase] for r in runs]
            yield {
                "workload": workload,
                "method": method,
                "phase": phase,
                "repeats": len(runs),
                "judgments_per_repeat": judgments // len(runs),
                "accuracy_percent": round(100 * sum(s["matches_at_0_5"] for s in scores) / judgments, 3),
                "resolved_percent": round(100 * sum(s["resolved"] for s in scores) / judgments, 3),
                "correct_resolved": sum(s["correct_resolved"] for s in scores),
                "resolved": sum(s["resolved"] for s in scores),
                "unknown": sum(s["unknown"] for s in scores),
                "not_evaluated": sum(s["not_evaluated"] for s in scores),
                "median_ms": round(median(m["seconds"] for m in measurements) * 1000, 3),
                "min_ms": round(min(m["seconds"] for m in measurements) * 1000, 3),
                "max_ms": round(max(m["seconds"] for m in measurements) * 1000, 3),
                "warm_median_ms": round(median(r["warm_seconds"] for r in runs) * 1000, 3),
                "requests_mean": mean(m["requests"] for m in measurements),
                "input_tokens_mean": mean(m["input_tokens"] for m in measurements),
                "http_failures": sum(m["http_failures"] for m in measurements),
                "warm_equal": all(r["warm_equal"] for r in runs),
                "warm_requests": sum(r["passes"]["warm"]["requests"] for r in runs),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not report["live"] or not report["runs"]:
        parser.error("A nonempty live evaluation is required")
    rows = list(summarize(report))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} aggregate rows to {args.output}")


if __name__ == "__main__":
    main()
