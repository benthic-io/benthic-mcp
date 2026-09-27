import json
from pathlib import Path
from typing import Any

from promote import Arm, evaluate, load_arm


def make_arm(name: str, results: list[dict[str, Any]]) -> Arm:
    return load_arm(name, _write(name, results))


def _write(name: str, results: list[dict[str, Any]]) -> Path:
    directory = Path("/tmp/benthic-gate-tests") / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.json").write_text(json.dumps(results), encoding="utf-8")
    return directory


def case(case_id: str, passed: bool, calls: int = 3, elapsed: int = 1000, hits: list[str] | None = None) -> dict:
    return {
        "id": case_id,
        "score": {"passed": passed, "forbidden_claim_hits": hits or []},
        "events": [{} for _ in range(calls)],
        "elapsed_ms": elapsed,
    }


def test_a_better_candidate_passes() -> None:
    baseline = make_arm("baseline-better", [case("a", False), case("b", True)])
    candidate = make_arm("candidate-better", [case("a", True), case("b", True)])

    verdict = evaluate(baseline, candidate)

    assert verdict["passed"] is True
    assert verdict["pass_rate_gain"] == 0.5


def test_an_unchanged_candidate_is_rejected() -> None:
    baseline = make_arm("baseline-same", [case("a", True), case("b", True)])
    candidate = make_arm("candidate-same", [case("a", True), case("b", True)])

    verdict = evaluate(baseline, candidate)

    assert verdict["passed"] is False
    assert "pass_rate_improves" in [check["name"] for check in verdict["checks"] if not check["ok"]]


def test_a_case_that_regresses_is_rejected() -> None:
    baseline = make_arm("baseline-regress", [case("a", False), case("b", True), case("c", True)])
    candidate = make_arm("candidate-regress", [case("a", True), case("b", False), case("c", True)])

    verdict = evaluate(baseline, candidate)

    assert verdict["passed"] is False
    assert verdict["regressions"] == ["b"]


def test_a_new_forbidden_claim_is_rejected() -> None:
    baseline = make_arm("baseline-claims", [case("a", False), case("b", True)])
    candidate = make_arm("candidate-claims", [case("a", True, hits=["unsigned join"]), case("b", True)])

    verdict = evaluate(baseline, candidate)

    assert verdict["passed"] is False
    assert verdict["new_forbidden_claims"] == ["a"]


def test_more_tool_calls_are_rejected_beyond_the_budget() -> None:
    baseline = make_arm("baseline-calls", [case("a", False, calls=10), case("b", True, calls=10)])
    candidate = make_arm("candidate-calls", [case("a", True, calls=13), case("b", True, calls=13)])

    verdict = evaluate(baseline, candidate)

    assert verdict["passed"] is False
    assert verdict["tool_call_growth"] > 0.10


def test_slightly_fewer_tool_calls_are_allowed() -> None:
    baseline = make_arm("baseline-fewer", [case("a", False, calls=10), case("b", True, calls=10)])
    candidate = make_arm("candidate-fewer", [case("a", True, calls=9), case("b", True, calls=9)])

    assert evaluate(baseline, candidate)["passed"] is True


def test_a_large_latency_regression_is_rejected() -> None:
    baseline = make_arm("baseline-latency", [case("a", False), case("b", True)])
    candidate = make_arm("candidate-latency", [case("a", True, elapsed=5000), case("b", True, elapsed=5000)])

    verdict = evaluate(baseline, candidate)

    assert verdict["passed"] is False
    assert verdict["latency_growth"] > 0.20


def test_p95_latency_ignores_a_single_outlier() -> None:
    arm = make_arm(
        "arm-p95",
        [case(f"c{index}", True, elapsed=1000) for index in range(19)] + [case("slow", True, elapsed=90000)],
    )

    assert arm.p95_latency == 1000


def test_metrics_ignore_cases_missing_from_one_arm() -> None:
    baseline = make_arm("baseline-missing", [case("a", True), case("b", True)])
    candidate = make_arm("candidate-missing", [case("a", True)])

    verdict = evaluate(baseline, candidate)

    assert verdict["regressions"] == ["b"]
