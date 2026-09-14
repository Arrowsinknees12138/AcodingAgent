"""Run the deterministic E2E suite and emit comparable JSON metrics."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "benchmarks" / "cases.json"
TEST_FILE = ROOT / "tests" / "e2e" / "test_full_pipeline.py"
NUMERIC_PROPERTIES = {
    "model_calls": int,
    "input_tokens": int,
    "output_tokens": int,
    "model_cost_usd": float,
    "sandbox_seconds": float,
    "duration_seconds": float,
    "repair_rounds": int,
    "changed_paths_count": int,
}


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(fraction * len(ordered)) - 1
    return ordered[max(0, rank)]


def parse_junit(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Convert pytest JUnit into a stable, per-scenario benchmark report."""
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("JUnit XML exceeds 16 MiB")
    document = path.read_bytes()
    if b"<!DOCTYPE" in document.upper() or b"<!ENTITY" in document.upper():
        raise ValueError("JUnit XML must not contain DTDs or entities")
    root = ET.fromstring(document)  # noqa: S314 - bounded local XML without DTDs/entities
    observed: dict[str, dict[str, Any]] = {}
    for case in root.iter("testcase"):
        node_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        name = case.get("name", "")
        properties = {
            prop.get("name", ""): prop.get("value", "")
            for prop in case.findall("./properties/property")
        }
        for field, converter in NUMERIC_PROPERTIES.items():
            if field in properties:
                properties[field] = converter(properties[field])
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        status = (
            "failed"
            if failure is not None
            else "skipped"
            if case.find("skipped") is not None
            else "passed"
        )
        observed[name] = {
            "node_id": node_id,
            "test_status": status,
            "test_duration_seconds": float(case.get("time", "0")),
            "failure": failure.get("message", "") if failure is not None else None,
            "metrics": properties,
        }

    scenarios: list[dict[str, Any]] = []
    for spec in manifest["scenarios"]:
        scenario_case = observed.get(spec["pytest_name"])
        scenarios.append(
            {
                "id": spec["id"],
                "description": spec["description"],
                **(scenario_case or {"test_status": "missing", "metrics": {}}),
            }
        )

    passed = [case for case in scenarios if case["test_status"] == "passed"]
    durations = [case["test_duration_seconds"] for case in passed]
    metric_rows = [case["metrics"] for case in passed]
    count = len(scenarios)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scenarios": scenarios,
        "summary": {
            "total": count,
            "passed": len(passed),
            "failed": sum(case["test_status"] == "failed" for case in scenarios),
            "skipped": sum(case["test_status"] == "skipped" for case in scenarios),
            "missing": sum(case["test_status"] == "missing" for case in scenarios),
            "success_rate": len(passed) / count if count else 0.0,
            "mean_test_duration_seconds": mean(durations) if durations else None,
            "p95_test_duration_seconds": _percentile(durations, 0.95),
            "total_model_calls": sum(row.get("model_calls", 0) for row in metric_rows),
            "total_model_cost_usd": sum(row.get("model_cost_usd", 0.0) for row in metric_rows),
            "total_sandbox_seconds": sum(row.get("sandbox_seconds", 0.0) for row in metric_rows),
        },
    }


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, float | None]:
    """Compute current-minus-baseline deltas for headline metrics."""
    fields = (
        "success_rate",
        "mean_test_duration_seconds",
        "p95_test_duration_seconds",
        "total_model_calls",
        "total_model_cost_usd",
        "total_sandbox_seconds",
    )
    before = baseline["summary"]
    after = current["summary"]
    return {
        field: (after[field] - before[field])
        if after.get(field) is not None and before.get(field) is not None
        else None
        for field in fields
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path")
    parser.add_argument("--baseline", type=Path, help="Prior JSON report to compare")
    parser.add_argument(
        "--junit", type=Path, help="Parse an existing JUnit XML instead of running E2E"
    )
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    manifest = json.loads(CASES.read_text(encoding="utf-8"))
    if args.junit:
        report = parse_junit(args.junit, manifest)
        exit_code = 0 if report["summary"]["success_rate"] == 1 else 1
    else:
        with tempfile.TemporaryDirectory(prefix="repopilot-benchmark-") as temp:
            junit = Path(temp) / "results.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                str(TEST_FILE),
                f"--junitxml={junit}",
                "-o",
                "junit_family=xunit1",
                "-q",
            ]
            completed = subprocess.run(command, cwd=ROOT, check=False)  # noqa: S603
            exit_code = completed.returncode
            if not junit.exists():
                print("pytest did not produce JUnit XML", file=sys.stderr)
                return exit_code or 2
            report = parse_junit(junit, manifest)
    if args.baseline:
        prior = json.loads(args.baseline.read_text(encoding="utf-8"))
        report["delta_from_baseline"] = compare(prior, report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report: {args.output}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
