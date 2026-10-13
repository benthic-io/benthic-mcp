"""Contracts for the regrade path.

Re-grading a stored run is free: it applies the current grader and the current corpus values to
recorded transcripts without spending model time. That makes it the cheapest verification in the
project - but only if regrading never changes a verdict by itself. These contracts pin both
directions: a current file round-trips byte-identical, and a stale file picks up exactly the
current verdicts and nothing else.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval" / "truth"))
sys.path.insert(0, str(ROOT / "src"))
_spec = importlib.util.spec_from_file_location("grade", ROOT / "eval" / "truth" / "grade.py")
assert _spec is not None and _spec.loader is not None
grade = importlib.util.module_from_spec(_spec)
sys.modules["grade"] = grade
_spec.loader.exec_module(grade)
_suite_spec = importlib.util.spec_from_file_location("run_suite", ROOT / "eval" / "truth" / "run_suite.py")
assert _suite_spec is not None and _suite_spec.loader is not None
run_suite = importlib.util.module_from_spec(_suite_spec)
sys.modules["run_suite"] = run_suite
_suite_spec.loader.exec_module(run_suite)


def case(case_id: str, value: float) -> dict[str, Any]:
    return {
        "id": case_id,
        "capability": "numeric_aggregate",
        "question": "q",
        "expected": {"relation": "a.b", "kind": "count", "value": value},
        "forbidden_claims": [],
    }


def run_record(case_id: str, answer: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "capability": "numeric_aggregate",
        "question": "q",
        "answer": answer,
        "turns": [{"turn": 1, "reasoning": answer}],
        "elapsed_s": 1.0,
        "answered": True,
    }


def write_run(path: Path, runs: list[dict[str, Any]], stale_results: Any = None) -> None:
    document: dict[str, Any] = {"runs": runs}
    if stale_results is not None:
        document["results"] = stale_results
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def test_regrade_replaces_stale_verdicts_with_current_ones(tmp_path: Path) -> None:
    """A recorded pass that the current grader still passes, and a recorded fail it still fails,
    come back with their current verdicts even when the file's results section says otherwise."""
    cases = [case("pass", 42.0), case("fail", 42.0)]
    runs = [run_record("pass", "the answer is 42 rows."), run_record("fail", "the answer is 43 rows.")]
    stale = [
        {"id": "pass", "capability": "numeric_aggregate", "passed": False, "checks": []},
        {"id": "fail", "capability": "numeric_aggregate", "passed": True, "checks": []},
    ]
    run_path = tmp_path / "run.json"
    write_run(run_path, runs, stale_results=stale)

    summary = run_suite.regrade_file(run_path, cases, None, None)

    document = json.loads(run_path.read_text(encoding="utf-8"))
    assert [r["passed"] for r in document["results"]] == [True, False]
    assert summary["cases"] == 2
    assert summary["by_capability"]["numeric_aggregate"]["passed"] == 1


def test_regrade_of_a_current_file_is_byte_identical(tmp_path: Path) -> None:
    """Re-grading must change nothing by itself: the second regrade of the same file with the same
    grader and cases is byte-identical. A verdict that flips on regrade names a grader or corpus
    change, never the tool."""
    cases = [case("pass", 42.0)]
    runs = [run_record("pass", "the answer is 42 rows.")]
    run_path = tmp_path / "run.json"
    write_run(run_path, runs)

    run_suite.regrade_file(run_path, cases, None, None)
    once = run_path.read_bytes()
    run_suite.regrade_file(run_path, cases, None, None)
    assert run_path.read_bytes() == once


def test_regrade_reads_scientific_notation_answers(tmp_path: Path) -> None:
    """Regression pin for the e-notation fix at the tool level: a recorded answer the server
    taught the model to write must grade as a pass through the regrade path, not only through
    the parser unit test."""
    value = 1.0000000001534684e18
    cases = [case("big", value)]
    runs = [run_record("big", "the maximum is 1.0000000001534684e+18")]
    run_path = tmp_path / "run.json"
    write_run(run_path, runs)

    run_suite.regrade_file(run_path, cases, None, None)

    document = json.loads(run_path.read_text(encoding="utf-8"))
    assert document["results"][0]["passed"] is True


def test_regrade_rejects_a_file_with_no_runs(tmp_path: Path) -> None:
    """The tool states its scope: a file without a runs list is not regradable, and that is an
    error rather than an empty pass."""
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps({"results": []}, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="runs"):
        run_suite.regrade_file(run_path, [], None, None)
