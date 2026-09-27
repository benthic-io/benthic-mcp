"""The tripwire's decision logic, tested on synthetic arms so the rollback rule is pinned down."""

from pathlib import Path

from guard import Arm, compare, lessons_since, render

from benthic_mcp.playbook import LessonRecord, Playbook
from benthic_mcp.seed import seed_playbook


def arm(name: str, cases: dict[str, list[bool]]) -> Arm:
    result = Arm(name=name, playbook=f"/{name}.json")
    result.per_case = cases
    return result


def test_one_case_losing_one_rep_of_two_is_noise_not_damage() -> None:
    good = arm("good", {"a": [True, True], "b": [True, True]})
    same = arm("same", {"a": [True, True], "b": [True, False]})

    verdict = compare(good, same)

    assert verdict["regressed"] is False
    assert verdict["aggregate_dropped"] is True
    assert verdict["regressions"] == [{"case": "b", "known_good": "2/2", "candidate": "1/2"}]


def test_two_cases_getting_worse_with_a_lower_aggregate_is_damage() -> None:
    good = arm("good", {"a": [True, True], "b": [True, True], "c": [True, True]})
    worse = arm("worse", {"a": [True, False], "b": [True, False], "c": [True, True]})

    verdict = compare(good, worse)

    assert verdict["regressed"] is True
    assert {entry["case"] for entry in verdict["regressions"]} == {"a", "b"}


def test_a_case_collapsing_from_all_pass_to_all_fail_is_damage_on_its_own() -> None:
    good = arm("good", {"a": [True, True], "b": [True, True]})
    collapsed = arm("collapsed", {"a": [True, True], "b": [False, False]})

    verdict = compare(good, collapsed)

    assert verdict["regressed"] is True
    assert verdict["collapses"] == [{"case": "b", "known_good": "2/2", "candidate": "0/2"}]


def test_an_improvement_is_not_a_regression() -> None:
    good = arm("good", {"a": [False, False]})
    better = arm("better", {"a": [True, True]})

    assert compare(good, better)["regressed"] is False
    assert compare(good, better)["pass_rate_delta"] == 1.0


def test_a_case_present_only_in_the_candidate_is_not_counted_as_a_regression() -> None:
    good = arm("good", {"a": [True]})
    extra = arm("extra", {"a": [True], "b": [False]})

    assert compare(good, extra)["regressed"] is False


def lesson(lesson_id: str) -> LessonRecord:
    return LessonRecord(lesson_id=lesson_id, symptom="a", lesson="b")


def test_only_lessons_added_since_the_known_good_would_be_quarantined() -> None:
    good = Playbook(collection="ngopen", lessons=[lesson("old1"), lesson("old2")])
    candidate = Playbook(collection="ngopen", lessons=[lesson("old1"), lesson("old2"), lesson("new1")])

    assert lessons_since(good, candidate) == ["new1"]


def test_the_verdict_states_its_own_limits() -> None:
    verdict = {
        "decided_at": "now",
        "known_good": "g.json",
        "candidate": "c.json",
        "comparison": compare(arm("g", {"a": [True]}), arm("c", {"a": [True]})),
        "lessons_added_since_known_good": [],
        "quarantined_lessons": [],
        "rolled_back": False,
        "held": True,
        "outcome": "HELD",
    }

    report = render(verdict)

    assert "tripwire against gross regression" in report
    assert "not a measurement of improvement" in report
    assert "clean tool trace" in report
    assert "| a | 1/1 | 1/1 |" in report


def test_a_regression_is_marked_in_the_table() -> None:
    verdict = {
        "decided_at": "now",
        "known_good": "g.json",
        "candidate": "c.json",
        "comparison": compare(arm("g", {"a": [True, True]}), arm("c", {"a": [True, False]})),
        "lessons_added_since_known_good": ["x"],
        "quarantined_lessons": ["x"],
        "rolled_back": True,
        "held": False,
        "outcome": "ROLLED BACK",
    }

    report = render(verdict)

    assert "**regressed**" in report
    assert "ROLLED BACK" in report
    assert "- x" in report


def test_a_saturated_holdout_is_inconclusive_rather_than_a_pass() -> None:
    # Observed in the real run: both arms passed 10/10 while the full suite had lost four cases.
    # Treating that as "no regression" and advancing known good is how a degraded playbook is
    # blessed, so saturation has to be reported as uninformative.
    good = arm("good", {f"c{index}": [True, True] for index in range(5)})
    same = arm("same", {f"c{index}": [True, True] for index in range(5)})

    verdict = compare(good, same)

    assert verdict["saturated"] is True
    assert verdict["regressed"] is False


def test_a_non_saturated_holdout_is_not_reported_as_saturated() -> None:
    good = arm("good", {"a": [True, True], "b": [True, False]})
    same = arm("same", {"a": [True, True], "b": [True, False]})

    assert compare(good, same)["saturated"] is False


def test_saturation_is_reported_in_the_verdict_and_the_report() -> None:
    verdict = {
        "decided_at": "now",
        "known_good": "g.json",
        "candidate": "c.json",
        "comparison": compare(arm("g", {"a": [True, True]}), arm("c", {"a": [True, True]})),
        "lessons_added_since_known_good": [],
        "quarantined_lessons": [],
        "rolled_back": False,
        "held": False,
        "outcome": "INCONCLUSIVE (holdout saturated)",
    }

    report = render(verdict)

    assert "INCONCLUSIVE" in report
    assert "holdout is saturated" in report
    assert "was deliberately not advanced" in report


def test_the_seed_is_the_first_run_baseline_rather_than_the_candidate(tmp_path: Path) -> None:
    # Round 3 of the first experiment: the guard had no known-good on record, copied the freshly
    # consolidated candidate over, and returned. That blessed the very change it existed to check,
    # so the round's core rules were never measured.
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    served = Playbook(collection="ngopen", core=["A candidate rule that was never validated."])
    (cache / "playbook.json").write_text(served.to_json(), encoding="utf-8")

    good_path = cache / "playbook.known-good.json"
    if not good_path.is_file():
        good_path.write_text(seed_playbook().to_json(), encoding="utf-8")

    known_good = Playbook.from_json(good_path.read_text(encoding="utf-8"))
    candidate = Playbook.from_json((cache / "playbook.json").read_text(encoding="utf-8"))

    # The baseline is the seed, so the unvalidated candidate core is not what gets blessed.
    assert known_good.core == seed_playbook().core
    assert known_good.core != candidate.core


def regressing_pair() -> tuple[Arm, Arm]:
    good = arm("good", {"a": [True, True], "b": [True, True], "c": [True, True]})
    worse = arm("worse", {"a": [True, False], "b": [True, False], "c": [True, True]})
    return good, worse


def test_a_detected_regression_is_never_reported_as_held() -> None:
    # The dry run detected the regression and the table said so, but the headline said HELD, which
    # is the same failure as a guard that no-ops quietly.
    good, worse = regressing_pair()
    verdict = {
        "decided_at": "now",
        "known_good": "g.json",
        "candidate": "c.json",
        "comparison": compare(good, worse),
        "lessons_added_since_known_good": ["x"],
        "quarantined_lessons": [],
        "rolled_back": False,
        "regression_detected": True,
        "held": False,
        "outcome": "REGRESSION DETECTED (dry run, no action taken)",
        "seeded_baseline": True,
    }

    report = render(verdict)

    assert "REGRESSION DETECTED" in report
    assert "HELD" not in report.replace("HELD IN PLACE", "")
    assert "**regressed**" in report


def test_the_verdict_separates_detection_from_action() -> None:
    good, worse = regressing_pair()
    comparison = compare(good, worse)

    assert comparison["regressed"] is True
    assert comparison["collapses"] == []
    assert comparison["aggregate_dropped"] is True
    assert comparison["saturated"] is False
