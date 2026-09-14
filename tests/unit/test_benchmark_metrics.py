from __future__ import annotations

from pathlib import Path

from benchmarks.run_e2e import compare, parse_junit


def test_parse_junit_records_missing_cases_and_metrics(tmp_path: Path) -> None:
    path = tmp_path / "results.xml"
    path.write_text(
        """<testsuite>
        <testcase name="success" classname="test_suite" time="2.5">
          <properties>
            <property name="model_calls" value="4"/>
            <property name="model_cost_usd" value="0.25"/>
            <property name="sandbox_seconds" value="1.5"/>
          </properties>
        </testcase>
        <testcase name="failure" classname="test_suite" time="1.0">
          <failure message="bad result"/>
        </testcase>
        </testsuite>""",
        encoding="utf-8",
    )
    manifest = {
        "scenarios": [
            {"id": "a", "pytest_name": "success", "description": "success"},
            {"id": "b", "pytest_name": "failure", "description": "failure"},
            {"id": "c", "pytest_name": "missing", "description": "missing"},
        ]
    }
    report = parse_junit(path, manifest)
    assert [case["test_status"] for case in report["scenarios"]] == [
        "passed",
        "failed",
        "missing",
    ]
    assert report["scenarios"][1]["failure"] == "bad result"
    assert report["summary"]["success_rate"] == 1 / 3
    assert report["summary"]["total_model_calls"] == 4
    assert report["summary"]["total_model_cost_usd"] == 0.25
    assert report["summary"]["p95_test_duration_seconds"] == 2.5


def test_compare_uses_current_minus_baseline() -> None:
    before = {"summary": {"success_rate": 0.5, "mean_test_duration_seconds": None}}
    after = {"summary": {"success_rate": 1.0, "mean_test_duration_seconds": 2.0}}
    delta = compare(before, after)
    assert delta["success_rate"] == 0.5
    assert delta["mean_test_duration_seconds"] is None
