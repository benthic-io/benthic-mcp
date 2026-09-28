"""Attribution decides whether a lesson earns its place, so its logic is tested without the LLM."""

from typing import Any

import pytest
from attrib import (
    confidence,
    core_lines_from,
    find_case,
    judge,
    without_lesson,
)

from benthic_mcp.playbook import LessonRecord, Playbook, load_playbook

COLUMN_LESSON = "Confirm a table's actual column names from the schema before querying instead of guessing"
SCAN_LESSON = "Deliver a final answer from a single complete scan rather than paginating with limit and offset"
# A shorter restatement of COLUMN_LESSON, which is what the consolidator's core distillation produces.
COLUMN_CORE_LINE = "Confirm a table's actual column names from the schema before querying"


def lesson(lesson_id: str, text: str, **overrides: Any) -> LessonRecord:
    payload: dict[str, Any] = {"lesson_id": lesson_id, "symptom": f"symptom for {text}", "lesson": text}
    payload.update(overrides)
    return LessonRecord(**payload)


def playbook(lessons: list[LessonRecord], core: list[str] | None = None) -> Playbook:
    return Playbook(collection="ngopen", generator="test", lessons=lessons, core=core or [])


def test_removing_a_lesson_also_drops_the_core_line_it_was_distilled_into() -> None:
    """Otherwise both arms still carry the advice and the comparison measures nothing."""
    document = playbook([lesson("a", COLUMN_LESSON), lesson("b", SCAN_LESSON)], core=[COLUMN_CORE_LINE])

    stripped, dropped, also = without_lesson(document, "a")

    assert [record.lesson_id for record in stripped.lessons] == ["b"]
    assert stripped.core == []
    assert dropped == [COLUMN_CORE_LINE]
    assert also == []


def test_an_unrelated_core_line_survives_removal() -> None:
    document = playbook([lesson("a", COLUMN_LESSON)], core=["Prefer the signed join path over a name match"])

    stripped, dropped, _ = without_lesson(document, "a")

    assert stripped.core == ["Prefer the signed join path over a name match"]
    assert dropped == []


def test_removal_does_not_mutate_the_served_document() -> None:
    document = playbook([lesson("a", COLUMN_LESSON), lesson("b", SCAN_LESSON)], core=[COLUMN_CORE_LINE])

    without_lesson(document, "a")

    assert len(document.lessons) == 2
    assert len(document.core) == 1


def test_asking_about_a_lesson_that_is_not_served_is_an_error() -> None:
    # Silently measuring a no-op arm would report "no effect" for a lesson that was never present.
    with pytest.raises(SystemExit, match="no lesson missing"):
        without_lesson(playbook([lesson("a", COLUMN_LESSON)]), "missing")


def test_core_overlap_counts_either_half_of_the_record() -> None:
    record = lesson("a", COLUMN_LESSON, symptom="Guessed a column name that does not exist in the table")

    assert core_lines_from([record], ["Guessed a column name that does not exist in the table"]) != []


def test_a_partial_improvement_is_called_a_fix_but_only_suggestive() -> None:
    outcome = judge([False] * 5, [True, True, False, True, False], min_delta=2)

    assert outcome["verdict"] == "fixes"
    assert outcome["delta"] == 3
    # A partial effect mixes within the arm that works, and that is the effect, not noise.
    assert outcome["unstable"] is True
    assert outcome["confidence"] == "suggestive"


def test_a_consistent_worsening_is_called_a_regression() -> None:
    assert judge([True] * 5, [False] * 5, min_delta=2)["verdict"] == "regresses"


def test_the_two_arms_are_genuinely_different_documents(tmp_path) -> None:
    """If both arms served the same playbook every lesson would read as having no effect."""
    served = playbook(
        [lesson("a", COLUMN_LESSON), lesson("b", SCAN_LESSON)],
        core=[COLUMN_CORE_LINE],
    )
    stripped, _, _ = without_lesson(served, "a")
    with_path = tmp_path / "with.json"
    without_path = tmp_path / "without.json"
    with_path.write_text(served.to_json(), encoding="utf-8")
    without_path.write_text(stripped.to_json(), encoding="utf-8")

    reloaded_with, status_with = load_playbook(with_path)
    reloaded_without, status_without = load_playbook(without_path)

    assert reloaded_with is not None and reloaded_without is not None
    assert (status_with, status_without) == ("active", "active")
    assert [record.lesson_id for record in reloaded_with.lessons] == ["a", "b"]
    assert [record.lesson_id for record in reloaded_without.lessons] == ["b"]
    assert with_path.read_text(encoding="utf-8") != without_path.read_text(encoding="utf-8")


UNKNOWN_LESSON = "When a query errors on unknown columns, re-run discovery to learn the real column names"
UNKNOWN_NEIGHBOUR = "When a query fails on an unknown column name, re-run discovery to learn the real column names"
UNKNOWN_CORE = "When a query errors on unknown columns, re-run discovery to learn the table's real columns"
UNKNOWN_NEIGHBOUR_CORE = "When a query fails on an unknown column name, re-run discovery to learn the real column names"
JOIN_CORE = "Only use joins listed in the signed join paths; never invent a join."


def test_a_redundant_neighbour_is_pulled_in_even_when_it_is_lexically_dissimilar() -> None:
    """The served core carried several near-identical unknown-column lines from different lessons.
    Removing one of those lessons while its neighbours keep restating the advice guarantees a false
    no-effect, so the whole advice cluster has to go."""
    document = playbook(
        [lesson("a", UNKNOWN_LESSON), lesson("b", UNKNOWN_NEIGHBOUR)],
        core=[UNKNOWN_CORE, UNKNOWN_NEIGHBOUR_CORE, JOIN_CORE],
    )

    stripped, removed, also = without_lesson(document, "a")

    assert also == ["b"]
    assert set(removed) == {UNKNOWN_CORE, UNKNOWN_NEIGHBOUR_CORE}
    assert stripped.core == [JOIN_CORE]


def test_removal_reaches_a_fixed_point_so_no_dropped_line_can_be_regenerated() -> None:
    """If a surviving lesson would distil a dropped line straight back, the arm is not clean."""
    document = playbook(
        [lesson("a", UNKNOWN_LESSON), lesson("b", UNKNOWN_NEIGHBOUR), lesson("c", SCAN_LESSON)],
        core=[UNKNOWN_CORE, UNKNOWN_NEIGHBOUR_CORE],
    )

    stripped, removed, _ = without_lesson(document, "a")
    survivors = [record.lesson_id for record in stripped.lessons]

    assert survivors == ["c"]
    assert not set(removed) & set(core_lines_from(stripped.lessons, removed))


def test_measuring_a_lesson_without_its_cohort_keeps_the_neighbour_records() -> None:
    # The core lines still go, because leaving them would put the advice back in the always-on
    # slice and the arm would measure nothing.
    document = playbook(
        [lesson("a", UNKNOWN_LESSON), lesson("b", UNKNOWN_NEIGHBOUR)],
        core=[UNKNOWN_CORE, UNKNOWN_NEIGHBOUR_CORE],
    )

    stripped, removed, also = without_lesson(document, "a", cohort=False)

    assert [record.lesson_id for record in stripped.lessons] == ["b"]
    assert also == []
    assert set(removed) == {UNKNOWN_CORE, UNKNOWN_NEIGHBOUR_CORE}


def test_a_shared_function_word_does_not_make_a_core_line_a_distillation() -> None:
    """Containment scored these as the same rule on the word "answer", which would have dropped a
    legitimate core line and left the "without" arm holding advice the lesson never contributed."""
    lesson_text = "Never end the session without delivering a final answer to the question that was asked"
    record = lesson("a", lesson_text)
    unrelated = [
        "Never use a current-only view to answer a historical question.",
        "Only use joins listed in the signed join paths; never invent a join.",
        "Dates in this catalog are ISO-8601; filter with gte/lte rather than string comparison.",
    ]

    assert core_lines_from([record], unrelated) == []


def test_a_real_distillation_of_the_lesson_is_still_recognised() -> None:
    record = lesson("a", "When a query errors on unknown columns, re-run discovery to learn the real column names")
    distilled = [
        "When a query errors on unknown columns, re-run discovery to learn the table's real columns before retrying"
    ]

    assert core_lines_from([record], distilled) == distilled


def test_a_saturated_case_cannot_show_an_effect_either_way() -> None:
    """ "No effect" and "nothing left to fix" are different, and conflating them throws good lessons away.

    The answer-delivery rule was measured as "no effect" on the case it came from, when that case
    already passed every repetition because the client had stopped deliberating. The rule was fine;
    the case had nothing left to fail.
    """
    outcome = judge([True] * 5, [True] * 5, min_delta=2)

    assert outcome["verdict"] == "no_failure"
    assert outcome["delta"] == 0


def test_a_lesson_that_breaks_a_passing_case_is_still_a_regression() -> None:
    # Checked before the saturated case: this is worse than useless, so it must not be reported as
    # "there was no failure to fix".
    assert judge([True] * 5, [False] * 5, min_delta=2)["verdict"] == "regresses"


def test_a_saturated_case_that_the_lesson_does_not_change_is_not_no_failure() -> None:
    # The case passes without the lesson but not reliably, so there was a failure to measure.
    assert (
        judge([True, True, True, False, True], [True, True, True, True, True], min_delta=2)["verdict"] != "no_failure"
    )


def test_a_one_rep_difference_is_never_readable_at_five_reps() -> None:
    # Any non-zero delta at 5 reps leaves one arm mixing, so a sub-threshold difference is always
    # inconclusive. This is the comparison the guard read as a regression from two of them.
    for without, with_lesson in (
        ([False] * 5, [True, False, False, False, False]),
        ([True] * 5, [True, True, True, True, False]),
        ([True, True, True, True, False], [True, False, True, True, False]),
    ):
        assert judge(without, with_lesson, min_delta=2)["verdict"] == "inconclusive"


def test_a_strong_improvement_is_distinguished_from_a_partial_one() -> None:
    assert judge([False] * 5, [True] * 5, min_delta=2)["confidence"] == "strong"


def test_an_every_rep_difference_is_strong_and_one_rep_is_none() -> None:
    assert confidence(5, 5, 2) == "strong"
    assert confidence(1, 5, 2) == "none"
    assert confidence(3, 5, 2) == "suggestive"


def test_a_partial_case_id_has_to_identify_one_case() -> None:
    cases = [
        {"id": "sequential_0_x", "capability": "sequential_lookup"},
        {"id": "sequential_1_x", "capability": "sequential_lookup"},
    ]

    with pytest.raises(SystemExit, match="matches 2 cases"):
        find_case(cases, "sequential_")
    assert find_case(cases, "sequential_0")["id"] == "sequential_0_x"


def test_a_case_id_that_matches_nothing_is_an_error() -> None:
    with pytest.raises(SystemExit, match="no case matching"):
        find_case([{"id": "a", "capability": "x"}], "zzz")
