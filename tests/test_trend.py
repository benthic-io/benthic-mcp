"""trend.py re-scores every round with the scorer as it stands, and had no tests.

Two things it does are easy to get silently wrong. It must exclude holdout cases, because a
playbook shaped by a case cannot then be credited or blamed for that case's score. And it must
degrade gracefully when a run directory has no results, since it is read while a run is still in
progress.
"""

import json
from pathlib import Path

import pytest
from trend import _cases, _passed, attach_rescored, rescore

CASES = [
    {"id": "tuning_a", "capability": "discovery", "question": "q", "required_tools": ["benthic_query"]},
    {"id": "held", "capability": "discovery", "question": "q", "required_tools": ["benthic_query"]},
]


def write_run(runs: Path, name: str, records: list[dict]) -> None:
    directory = runs / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.json").write_text(json.dumps(records), encoding="utf-8")


def record(case_id: str, passed: bool) -> dict:
    return {
        "id": case_id,
        "events": [{"name": "benthic_query", "ok": True, "arguments": {"source": "usaspending.all_entities"}}],
        "final_text": "an answer",
        "error": "",
        "score": {"passed": passed},
    }


@pytest.fixture
def bank(tmp_path: Path, monkeypatch) -> Path:
    """A question bank at the path trend.py reads, so re-scoring has cases to score against."""
    generated = tmp_path / "eval" / "generated"
    generated.mkdir(parents=True)
    (generated / "questions.json").write_text(json.dumps({"metadata": {}, "cases": CASES}), encoding="utf-8")
    monkeypatch.setattr("trend.ROOT", tmp_path)
    return tmp_path


def test_rescoring_recomputes_rather_than_trusting_the_recorded_score(bank: Path) -> None:
    """A scorer bug once understated five cases, and repeating the recorded number kept reporting it."""
    write_run(bank / "runs", "20260101T000000Z", [record("tuning_a", passed=False), record("held", passed=False)])

    scored = rescore(bank)

    # Both records claim failure, but the current scorer passes them on their events and text.
    assert scored == {"20260101T000000Z": {"tuning_a": True, "held": True}}


def test_rescore_is_none_when_there_are_no_runs(bank: Path) -> None:
    (bank / "runs").mkdir(parents=True, exist_ok=True)

    assert rescore(bank) is None


def test_rescore_skips_a_run_with_no_results_file(bank: Path) -> None:
    (bank / "runs" / "20260101T000000Z").mkdir(parents=True, exist_ok=True)

    assert rescore(bank) is None


def test_rescore_ignores_a_run_holding_a_case_the_bank_does_not_define(bank: Path) -> None:
    write_run(bank / "runs", "20260101T000000Z", [record("retired_case", True)])

    assert rescore(bank) is None


def test_a_holdout_failure_does_not_reduce_the_rescored_count(bank: Path) -> None:
    run = bank / "runs" / "20260101T000000Z"
    write_run(bank / "runs", run.name, [record("tuning_a", True), record("held", True)])

    enriched = attach_rescored([{"round": 1, "usage_run": str(run), "holdout_cases": ["held"]}], bank)

    assert enriched[0]["rescored_passed"] == 1
    assert enriched[0]["rescored_cases"] == 1


def test_a_tuning_failure_is_still_reported_as_failing(bank: Path) -> None:
    run = bank / "runs" / "20260101T000000Z"
    unanswered = record("tuning_a", True)
    unanswered["final_text"] = ""
    write_run(bank / "runs", run.name, [unanswered, record("held", True)])

    enriched = attach_rescored([{"round": 1, "usage_run": str(run), "holdout_cases": ["held"]}], bank)

    assert enriched[0]["rescored_failing"] == ["tuning_a"]


def test_a_round_with_no_recorded_holdout_scores_everything(bank: Path) -> None:
    run = bank / "runs" / "20260101T000000Z"
    write_run(bank / "runs", run.name, [record("tuning_a", True), record("held", True)])

    enriched = attach_rescored([{"round": 1, "usage_run": str(run)}], bank)

    assert enriched[0]["rescored_passed"] == 2
    assert enriched[0]["rescored_cases"] == 2


def test_a_round_whose_run_directory_is_gone_is_passed_through(bank: Path) -> None:
    enriched = attach_rescored([{"round": 1, "usage_run": str(bank / "runs" / "missing")}], bank)

    assert "rescored_passed" not in enriched[0]


def test_passed_and_cases_prefer_the_rescored_values() -> None:
    record = {"rescored_passed": 5, "rescored_cases": 5, "tuning_passed": 3, "tuning_cases": 4}

    assert _passed(record) == 5
    assert _cases(record) == 5


def test_passed_and_cases_still_read_the_original_field_name() -> None:
    # Archived runs recorded strict_passed before the tuning-only split existed.
    old = {"strict_passed": 28, "cases": 33}

    assert _passed(old) == 28
    assert _cases(old) == 33


@pytest.mark.parametrize("empty", [{}, {"round": 1}])
def test_passed_and_cases_tolerate_a_record_with_nothing_in_it(empty: dict) -> None:
    assert _passed(empty) == 0
    assert _cases(empty) == 0
