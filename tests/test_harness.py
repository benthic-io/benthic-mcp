import json
from pathlib import Path

import pytest
from harness import (
    CaseUsage,
    append_rounds,
    read_holdout_ids,
    rep_flip_rate,
    select_for_reflection,
    usage_from_result,
)


def record(case_id: str = "c1", capability: str = "discovery", **overrides) -> dict:
    payload = {
        "id": case_id,
        "capability": capability,
        "question": "which district did the CA representative run in during 2019",
        "events": [
            {
                "name": "benthic_query",
                "arguments": {"source": "usp_cl.mv_current_lawmakers", "where": ["state=eq.CA"]},
                "ok": False,
                "error": "Unknown columns for usp_cl.mv_current_lawmakers: party_name",
                "structured": None,
            }
        ],
        "final_text": "I could not answer that.",
        "score": {"passed": False, "forbidden_claim_hits": []},
        "error": "",
    }
    payload.update(overrides)
    return payload


def test_usage_derives_objective_struggles_from_the_tool_trace() -> None:
    usage = usage_from_result(record())

    assert usage.signatures
    assert usage.signatures[0]["kind"] == "unknown_column"
    assert usage.signatures[0]["source"] == "usp_cl.mv_current_lawmakers"


def test_a_run_that_hit_the_turn_limit_is_a_struggle() -> None:
    usage = usage_from_result(record(error="maximum turns reached"))

    assert "max_turns" in [signature["kind"] for signature in usage.signatures]


def test_a_failed_call_without_a_result_still_yields_its_source() -> None:
    usage = usage_from_result(record())

    assert usage.signatures[0]["source"] == "usp_cl.mv_current_lawmakers"


def test_a_clean_run_yields_no_signatures() -> None:
    payload = record()
    payload["events"] = [
        {
            "name": "benthic_query",
            "arguments": {"source": "a.b"},
            "ok": True,
            "error": "",
            "structured": {"row_count": 3},
        }
    ]

    assert usage_from_result(payload).signatures == []


def failed_check(usage: CaseUsage) -> dict:
    return next(sig for sig in usage.signatures if sig["kind"] == "failed_check")


def test_a_strict_failure_with_a_clean_trace_still_becomes_a_struggle() -> None:
    # A confidently wrong answer produces no server-side signal, so the failed check is the only
    # remaining usage signal.
    payload = record()
    payload["events"] = [
        {
            "name": "benthic_query",
            "arguments": {"source": "a.b"},
            "ok": True,
            "error": "",
            "structured": {"row_count": 1},
        }
    ]
    payload["final_text"] = "The answer is 42."
    payload["score"] = {"passed": False, "answered": True, "row_count_check": False, "forbidden_claim_hits": []}

    usage = usage_from_result(payload)

    assert failed_check(usage)["kind"] == "failed_check"
    assert "never retrieved the source" in failed_check(usage)["detail"]


def test_the_failed_check_detail_never_names_the_expected_relation() -> None:
    payload = record()
    payload["score"] = {"passed": False, "row_count_check": False, "forbidden_claim_hits": []}
    payload["expected"] = {"relation": "legislator_terms"}

    assert "legislator_terms" not in failed_check(usage_from_result(payload))["detail"]


def test_a_required_tool_may_be_named_because_it_is_not_the_answer() -> None:
    payload = record()
    payload["score"] = {"passed": False, "tool_requirement": False, "forbidden_claim_hits": []}
    payload["required_tools"] = ["benthic_join"]

    assert "benthic_join" in failed_check(usage_from_result(payload))["detail"]


def test_forbidden_claims_are_surfaced_verbatim() -> None:
    payload = record()
    payload["score"] = {"passed": False, "forbidden_claim_hits": ["unsigned join"]}

    assert "unsigned join" in failed_check(usage_from_result(payload))["detail"]


def test_a_passing_clean_run_produces_no_failed_check_signature() -> None:
    payload = record()
    payload["events"] = [
        {
            "name": "benthic_query",
            "arguments": {"source": "a.b"},
            "ok": True,
            "error": "",
            "structured": {"row_count": 3},
        }
    ]
    payload["score"] = {"passed": True, "answered": True, "forbidden_claim_hits": []}

    assert usage_from_result(payload).signatures == []


def test_a_quiet_failure_is_selected_for_reflection_even_without_a_tool_struggle() -> None:
    quiet_failure = CaseUsage(case_id="quiet", capability="discovery", passed=False, error="", signatures=[])
    clean_pass = CaseUsage(case_id="clean", capability="discovery", passed=True, error="", signatures=[])

    assert select_for_reflection([quiet_failure, clean_pass], 6) == [quiet_failure]


def test_only_struggling_sessions_are_selected_for_reflection() -> None:
    clean = CaseUsage(case_id="clean", capability="discovery", passed=True, error="", signatures=[])
    messy = CaseUsage(
        case_id="messy",
        capability="discovery",
        passed=False,
        error="",
        signatures=[{"kind": "unknown_column", "severity": 4}],
    )

    assert select_for_reflection([clean, messy], 6) == [messy]


def test_reflection_budget_is_spent_on_the_worst_first() -> None:
    low = CaseUsage(
        case_id="low", capability="x", passed=False, error="", signatures=[{"kind": "truncated", "severity": 2}]
    )
    high = CaseUsage(
        case_id="high", capability="x", passed=False, error="", signatures=[{"kind": "unknown_column", "severity": 4}]
    )

    assert [usage.case_id for usage in select_for_reflection([low, high], 1)] == ["high"]


def test_holdout_cases_are_never_reflected_on() -> None:
    """A lesson extracted from a case and then scored on it makes the score a training number."""
    held_out = CaseUsage(
        case_id="multi_step_0_0", capability="multi_step", passed=False, error="", signatures=[{"severity": 4}]
    )
    tuning = CaseUsage(
        case_id="discover_0", capability="discovery", passed=False, error="", signatures=[{"severity": 4}]
    )

    selected = select_for_reflection([held_out, tuning], 6, frozenset({"multi_step_0_0"}))

    assert [usage.case_id for usage in selected] == ["discover_0"]


def test_a_holdout_pass_is_not_reflected_on_either() -> None:
    passing = CaseUsage(case_id="join_0", capability="join", passed=True, error="", signatures=[{"severity": 4}])

    assert select_for_reflection([passing], 6, frozenset({"join_0"})) == []


def test_reps_that_agree_are_not_reported_as_flaky() -> None:
    records = [record(case_id="steady", score={"passed": True}), record(case_id="steady", score={"passed": True})]

    assert rep_flip_rate(records)["flaky_cases"] == []


def test_a_case_that_flips_between_reps_is_reported_as_flaky() -> None:
    """The guard's entire -12.5pp verdict rested on two such flips, so they have to be visible."""
    records = [
        record(case_id="wobbly", score={"passed": True}),
        record(case_id="wobbly", score={"passed": False}),
    ]

    assert rep_flip_rate(records)["flaky_cases"] == ["wobbly"]


def test_flaky_share_ignores_cases_run_only_once() -> None:
    """A single-rep case cannot be flaky, and averaging it in would understate the noise floor."""
    records = [
        record(case_id="wobbly", score={"passed": True}),
        record(case_id="wobbly", score={"passed": False}),
        record(case_id="steady", score={"passed": True}),
        record(case_id="steady", score={"passed": True}),
        record(case_id="once", score={"passed": False}),
    ]

    report = rep_flip_rate(records)

    assert report["flaky_cases"] == ["wobbly"]
    assert report["flaky_share"] == 0.5


def test_a_deterministically_failing_case_is_stable_not_flaky() -> None:
    records = [record(case_id="hard", score={"passed": False}), record(case_id="hard", score={"passed": False})]

    assert rep_flip_rate(records) == {
        "per_case": {"hard": {"passes": 0, "reps": 2, "flaky": False}},
        "flaky_cases": [],
        "flaky_share": 0.0,
    }


def test_flaky_share_is_none_when_nothing_was_repeated() -> None:
    assert rep_flip_rate([record()])["flaky_share"] is None


def test_reusing_a_run_without_recorded_splits_is_refused(tmp_path: Path) -> None:
    """Falling back to an empty holdout would quietly put the holdout back into reflection."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_meta.json").write_text(json.dumps({"reps": 1}), encoding="utf-8")

    with pytest.raises(SystemExit, match="predates recorded splits"):
        read_holdout_ids(run_dir)


def test_a_run_with_no_metadata_at_all_is_refused(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(SystemExit, match="holdout membership is unknown"):
        read_holdout_ids(run_dir)


def test_holdout_membership_is_read_from_the_batch_metadata(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_meta.json").write_text(json.dumps({"holdout_ids": ["b", "a"]}), encoding="utf-8")

    assert read_holdout_ids(run_dir) == frozenset({"a", "b"})


def test_rounds_accumulate_rather_than_overwrite(tmp_path: Path) -> None:
    append_rounds(tmp_path, {"round": 1, "strict_pass_rate": 0.5})
    append_rounds(tmp_path, {"round": 2, "strict_pass_rate": 0.75})

    rounds = json.loads((tmp_path / "rounds.json").read_text(encoding="utf-8"))

    assert [entry["round"] for entry in rounds] == [1, 2]


def test_running_out_of_tokens_is_reported_as_its_own_struggle() -> None:
    """Distinct from running out of turns: more turns would not help, so the two need different fixes."""
    usage = usage_from_result(record(score={"passed": False}, finish_reason="length"))

    assert "token_exhausted" in [signature["kind"] for signature in usage.signatures]


def test_a_run_that_answered_within_budget_is_not_reported_as_exhausted() -> None:
    usage = usage_from_result(record(score={"passed": True}, finish_reason="stop", final_text="42"))

    assert "token_exhausted" not in [signature["kind"] for signature in usage.signatures]
