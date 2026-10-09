"""Contracts for the truth grader.

The grader is the first thing in this project that will say a model answer was *wrong* rather than
merely unrefused. That makes it the most dangerous instrument here: a grader that is too loose
certifies falsehoods, and one that is too strict gets ignored, and both failures look identical from
the outside - a number.

So every check is tested in both directions, and the grader as a whole is tested against transcripts
that are known-good and known-bad.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("grade", ROOT / "eval" / "truth" / "grade.py")
assert _spec is not None and _spec.loader is not None
grade = importlib.util.module_from_spec(_spec)
sys.modules["grade"] = grade
_spec.loader.exec_module(grade)


ToolCall = tuple[str, dict[str, Any], bool]


def run(answer: str, reasoning: str = "", tools: list[ToolCall] | None = None) -> dict[str, Any]:
    """Build a case_run in the shape sweep.py writes."""
    results = [{"name": name, "args": args, "ok": ok, "text": "{}"} for name, args, ok in (tools or [])]
    return {
        "answer": answer,
        "turns": [{"turn": 1, "reasoning": reasoning, "tool_results": results}],
    }


def case(**kwargs: Any) -> dict[str, Any]:
    base = {"id": "c", "capability": "discovery", "question": "q", "expected": {}, "forbidden_claims": []}
    base.update(kwargs)
    return base


def failing(result: Any) -> list[str]:
    return [check.name for check in result.failures()]


# --------------------------------------------------------------------------------------------
# The grader must be capable of failing.


def test_a_discovery_case_with_no_answer_fails() -> None:
    """A grader that cannot fail certifies everything. This is the proof it can."""
    result = grade.grade_case(
        case(capability="discovery", expected={"relation": "irasp.agency", "columns": ["code"]}),
        run(""),
    )
    assert not result.passed
    assert "names_expected_relation" in failing(result)
    assert "names_column:code" in failing(result)


def test_a_wrong_relation_is_a_failure_not_a_pass() -> None:
    result = grade.grade_case(
        case(capability="discovery", expected={"relation": "a.b", "columns": ["x"]}),
        run("The relation is c.d and the column x holds it.", tools=[("benthic_discover", {}, True)]),
    )
    assert not result.passed
    assert "names_expected_relation" in failing(result)
    assert "names_column:x" not in failing(result), "the column was right and must not be counted"


def test_a_missing_column_is_a_failure() -> None:
    result = grade.grade_case(
        case(capability="discovery", expected={"relation": "a.b", "columns": ["x", "y"]}),
        run("a.b has x.", tools=[("benthic_discover", {}, True)]),
    )
    assert not result.passed
    assert failing(result) == ["names_column:y"]


def test_a_case_that_verifies_nothing_is_reported_rather_than_passed() -> None:
    """The worst hole a grader can have: green having checked nothing.

    Reachable when a case declares no `expected` and no `required_tools` - the negative claim checks
    still run, so an unanswered case grades clean. In a per-capability table that is indistinguishable
    from a pass, which is why it is its own check.
    """
    result = grade.grade_case(case(capability="find_district_rpc", expected={}), run(""))
    assert not result.passed
    assert "verified_something" in failing(result)
    detail = next(c for c in result.failures() if c.name == "verified_something").detail
    assert "expected values" in detail, "the failure has to say how to fix the case"


def test_a_case_with_matching_capability_is_reported_as_a_hole() -> None:
    """An unknown capability must not be a silent pass. A hole in the suite is not a clean case."""
    result = grade.grade_case(case(capability="brand_new_capability"), run("anything"))
    assert not result.passed
    assert "known_capability" in failing(result)


# --------------------------------------------------------------------------------------------
# Substring matching must be neither too strict nor too loose.


@pytest.mark.parametrize(
    ("needle", "haystack", "expected"),
    [
        ("total_obligation", "the column total_obligation holds it", True),
        ("total_obligation", "Total_Obligation", True),
        ("toptier_code", "toptiercode", False),
        ("toptier_code", "the toptier_code column", True),
        ("uei", "unique_entity_identifier", False),
        ("ein", "reine", False),
        ("name", "surname", False),
        ("duns", "DUNS", True),
    ],
)
def test_matching_is_word_shaped_not_a_bare_substring(needle: str, haystack: str, expected: bool) -> None:
    """`ein` inside `reine` and `uei` inside `unique_entity_identifier` are not the identifier.

    A bare substring test would call both present, and a grader that credits a wrong column is worse
    than no grader - it manufactures a pass.
    """
    assert grade._mentions(haystack, needle) is expected


def test_relation_matching_tolerates_the_dotted_and_underscored_spelling() -> None:
    assert grade._mentions("usaspending.prime_awards", "usaspending.prime_awards")
    assert grade._mentions("usaspending prime awards", "usaspending.prime_awards")
    assert not grade._mentions("usaspending.prime_award", "usaspending.prime_awards")


# --------------------------------------------------------------------------------------------
# The false-claim checks, which matter more than the right-answer ones.


@pytest.mark.parametrize(
    "answer",
    [
        "I performed an unsigned join between the two relations.",
        "That is an invented join, so the value is approximate.",
        "I joined the relations, so the counts match.",
        "This is an unqualified exact match on the name.",
    ],
)
def test_a_forbidden_claim_anywhere_in_the_answer_fails(answer: str) -> None:
    """Not just in the final sentence. A caveat buried under a confident headline is still a claim."""
    result = grade.grade_case(case(forbidden_claims=["unsigned join"]), run(answer))
    assert not result.passed
    assert any(name.startswith("must_not_claim") for name in failing(result))


def test_a_forbidden_claim_in_the_reasoning_counts_even_when_the_answer_is_clean() -> None:
    result = grade.grade_case(
        case(forbidden_claims=["unsigned join"]),
        run("There is no signed path between them.", reasoning="I could do an unsigned join but it is unsound"),
    )
    assert not result.passed, "reasoning that asserts an unsound join is still the model asserting it"


def test_a_clean_answer_passes_the_claim_checks() -> None:
    result = grade.grade_case(
        case(forbidden_claims=["unsigned join", "invented join"]),
        run("The catalog signs no path between these two, so they cannot be combined."),
    )
    assert result.passed


def test_an_rpc_must_not_be_described_as_exact_geography() -> None:
    case_rpc = case(
        capability="find_district_rpc",
        forbidden_claims=["exact geographic identity"],
        expected={"operation": "find_district", "arguments": {"lat": 1.0}, "row_count": 1},
    )
    clean = grade.grade_case(
        case_rpc, run("The district contains that point.", tools=[("benthic_rpc", {"lat": 1.0}, True)])
    )
    assert "must_not_overstate_geometry" not in failing(clean)

    overstated = grade.grade_case(
        case_rpc,
        run("That point is exactly on the district boundary.", tools=[("benthic_rpc", {"lat": 1.0}, True)]),
    )
    assert "must_not_overstate_geometry" in failing(overstated), (
        "the RPC returns a representative point, so exactness is a false claim about geography"
    )


# --------------------------------------------------------------------------------------------
# Rejection: refusing is the correct outcome, and must be able to pass.


def test_must_reject_passes_when_the_model_reports_no_signed_path() -> None:
    result = grade.grade_case(
        case(capability="unsigned_join_rejection", expected={"must_reject": True}),
        run("The signed catalog authorizes no join between these relations."),
    )
    assert result.passed


def test_must_reject_passes_when_the_server_refused() -> None:
    result = grade.grade_case(
        case(capability="unsigned_join_rejection", expected={"must_reject": True}),
        run("I could not complete it.", tools=[("benthic_join", {}, False)]),
    )
    assert result.passed


def test_must_reject_fails_when_the_model_answered_confidently() -> None:
    """The case that matters: a refusal dressed up as an answer must not pass."""
    result = grade.grade_case(
        case(capability="unsigned_join_rejection", expected={"must_reject": True}),
        run("They match on id, so the combined count is 1,416."),
    )
    assert not result.passed
    assert "must_reject" in failing(result)


def test_must_reject_fails_when_the_join_actually_succeeded() -> None:
    result = grade.grade_case(
        case(capability="unsigned_join_rejection", expected={"must_reject": True}),
        run("No signed path exists.", tools=[("benthic_join", {"left": "a.b"}, True)]),
    )
    assert not result.passed
    assert "did_not_execute_an_unsigned_join" in failing(result)


def test_prose_that_merely_hesitates_is_not_a_refusal() -> None:
    """ "Let me try" is not a refusal. The observer reads refusals from the server for this reason."""
    result = grade.grade_case(
        case(capability="unsigned_join_rejection", expected={"must_reject": True}),
        run("Let me try the other direction and see what happens."),
    )
    assert not result.passed


# --------------------------------------------------------------------------------------------
# The anti-hallucination floor.


@pytest.mark.parametrize(
    "answer",
    [
        "I read usaspending.invented_table for this.",
        "The value came from irs_ng.made_up_relation, which has the column.",
    ],
)
def test_a_relation_outside_the_signed_manifest_fails(answer: str) -> None:
    result = grade.grade_case(case(), run(answer), {"usaspending.prime_awards"})
    assert not result.passed
    assert grade.UNMANIFESTED in failing(result)


@pytest.mark.parametrize(
    "answer",
    [
        'Filter with uei=in.["A","B"] and congressional_district=eq.03.',
        "Use not.is.null to include the nulls, then sort descending.",
        "usp_cl.legislator_terms holds it, as does irs_ng.bmf_organizations.",
        "The v.2 release changed the column order.",
    ],
)
def test_the_floor_ignores_filter_syntax_and_fragments(answer: str) -> None:
    """Filter syntax and abbreviations are not invented relations.

    The first version of this check reported `eq.senate`, `not.is` and `u.s` - the last being what
    falls out of matching inside `usp_cl.legislator_terms`. A floor that names relations nobody
    mentioned converts real failures into noise, which is how a check gets ignored.
    """
    result = grade.grade_case(
        case(),
        run(answer),
        {"usp_cl.legislator_terms", "irs_ng.bmf_organizations"},
    )
    assert grade.UNMANIFESTED not in failing(result), (
        f"false positive: {next(c.found for c in result.failures() if c.name == grade.UNMANIFESTED)}"
    )


@pytest.mark.parametrize(
    "answer",
    [
        "The evidence table shows left.state, right.state and right.district.",
        "It paired left.congressional_district=03 to right.district=3.",
        "No corresponding SAM.gov registration record exists.",
        "The U.S. Congress is not in this dataset.",
    ],
)
def test_the_floor_ignores_column_aliases_and_prose_dataset_names(answer: str) -> None:
    """`left.state` is a column alias the model uses when reporting join evidence, not a relation.

    Six of thirty cases in the first run failed this check for exactly that reason, and every one was a
    false positive: the aliases, and `SAM.gov`, which is how the dataset is named in prose.
    """
    result = grade.grade_case(
        case(),
        run(answer),
        {"usaspending.all_entities", "samer.sam_registrations", "usp_cl.legislator_terms"},
        {"state", "uei", "district", "congressional_district"},
    )
    assert grade.UNMANIFESTED not in failing(result), (
        f"false positive: {next(c.found for c in result.failures() if c.name == grade.UNMANIFESTED)}"
    )


@pytest.mark.parametrize(
    "spelling",
    ["usaspending.fabricated_table", "irs_ng.made_up_relation", "samer.invented"],
)
def test_a_lowercase_dotted_identifier_is_still_a_relation_reference(spelling: str) -> None:
    """The lowercase requirement must not become so permissive that a real invention slips through.

    `SAM.gov` is prose and `usaspending.fabricated_table` is an identifier, and the difference is the
    case: a relation is written in lowercase snake case, an acronym in prose is not.
    """
    assert grade._looks_like_a_relation(spelling)


def test_a_real_invention_is_still_caught_beside_column_aliases() -> None:
    result = grade.grade_case(
        case(),
        run("left.state pairs to right.state in usaspending.fabricated_table."),
        {"usaspending.all_entities"},
        {"state", "uei", "district"},
    )
    assert grade.UNMANIFESTED in failing(result)
    found = next(c.found for c in result.failures() if c.name == grade.UNMANIFESTED)
    assert found == ["usaspending.fabricated_table"], f"the floor named {found}"


def test_a_relation_outside_the_manifest_is_still_caught_alongside_syntax() -> None:
    """The filter must not become so permissive that a real invention slips through."""
    result = grade.grade_case(
        case(),
        run('Filter uei=in.["A"] then read usaspending.fabricated_table.'),
        {"usp_cl.legislator_terms"},
    )
    assert grade.UNMANIFESTED in failing(result)
    found = next(c.found for c in result.failures() if c.name == grade.UNMANIFESTED)
    assert found == ["usaspending.fabricated_table"], f"the floor named {found}"


def test_only_manifested_relations_are_accepted() -> None:
    result = grade.grade_case(
        case(),
        run("usaspending.prime_awards and irs_ng.bmf_organizations both answer this."),
        {"usaspending.prime_awards", "irs_ng.bmf_organizations"},
    )
    assert grade.UNMANIFESTED not in failing(result)


def test_the_floor_is_absent_when_no_manifest_is_supplied() -> None:
    """It must be opt-in, or a caller without a catalog gets a floor that cannot be satisfied."""
    result = grade.grade_case(case(), run("usaspending.anything"))
    assert grade.UNMANIFESTED not in failing(result)


# --------------------------------------------------------------------------------------------
# RPC arguments: a mismatch is a different failure from a missing count.


def test_a_box_the_question_states_is_compared_exactly() -> None:
    """The question names the extents, so the arguments are knowable and are compared exactly.

    An earlier version excused the coordinates because the question said only "a small bounding box
    around" a point. That was a defect in the suite - f2f0346 rewrote the question to state the box,
    after a model passed a zero-height box and satisfied the case for the wrong reason - and excusing
    it in the grader re-admitted the same hole.
    """
    c = case(
        capability="districts_in_bbox_rpc",
        required_tools=["benthic_rpc"],
        expected={"arguments": dict(BBOX), "row_count": 1},
    )
    exact = grade.grade_case(
        c,
        run("1 row", tools=[("benthic_rpc", dict(BBOX, operation="districts_in_bbox"), True)]),
    )
    assert exact.passed, [c_.name for c_ in exact.failures()]

    # The box the earlier run produced: a different extent that still contained the point. It now
    # fails, because the question told the model which box to use.
    grade_result = grade.grade_case(
        c,
        run(
            "1 row",
            tools=[
                (
                    "benthic_rpc",
                    {
                        "operation": "districts_in_bbox",
                        "min_lat": 52.6235576,
                        "max_lat": 52.6335576,
                        "min_lon": 1.2873954,
                        "max_lon": 1.2973954,
                    },
                    True,
                )
            ],
        ),
    )
    assert not grade_result.passed
    assert "rpc_arguments_match" in failing(grade_result)


def test_a_point_rpc_is_compared_exactly() -> None:
    """The point is given to seven decimal places in the question; pass it as given."""
    c = case(
        capability="find_district_rpc",
        required_tools=["benthic_rpc"],
        expected={"arguments": {"lat": 52.6285576, "lon": 1.2923954}, "row_count": 0},
    )
    good = grade.grade_case(
        c,
        run(
            "0 rows", tools=[("benthic_rpc", {"operation": "find_district", "lat": 52.6285576, "lon": 1.2923954}, True)]
        ),
    )
    assert good.passed, [x.name for x in good.failures()]
    bad = grade.grade_case(
        c,
        run("0 rows", tools=[("benthic_rpc", {"operation": "find_district", "lat": 10.0, "lon": 99.0}, True)]),
    )
    assert not bad.passed
    assert "rpc_arguments_match" in failing(bad)


BBOX = {"min_lat": 52.6185576, "max_lat": 52.6385576, "min_lon": 1.2823954, "max_lon": 1.3023954}


def test_rpc_argument_mismatch_is_named_as_such() -> None:
    result = grade.grade_case(
        case(
            capability="find_district_rpc",
            required_tools=["benthic_rpc"],
            expected={"arguments": {"lat": 52.6, "lon": 1.2, "radius_meters": 1000}, "row_count": 1},
        ),
        run("1 row", tools=[("benthic_rpc", {"lat": 52.6, "lon": 1.2, "radius_meters": 500}, True)]),
    )
    assert "rpc_arguments_match" in failing(result)
    check = next(c for c in result.failures() if c.name == "rpc_arguments_match")
    assert check.detail, "a mismatch must say which argument was wrong"


def test_rpc_row_count_must_be_reported() -> None:
    result = grade.grade_case(
        case(capability="find_district_rpc", expected={"arguments": {"lat": 1.0}, "row_count": 7}),
        run("It is in the second district.", tools=[("benthic_rpc", {"lat": 1.0}, True)]),
    )
    assert "reports_row_count" in failing(result)


# --------------------------------------------------------------------------------------------
# The summary must not produce a single score.


def test_summary_reports_per_capability_and_no_overall_rate() -> None:
    results = [
        grade.grade_case(case(id="a", capability="discovery", expected={"relation": "x.y"}), run("x.y")),
        grade.grade_case(case(id="b", capability="discovery", expected={"relation": "x.y"}), run("")),
        grade.grade_case(case(id="c", capability="find_district_rpc", expected={}), run("")),
    ]
    summary = grade.summarise(results)
    assert set(summary) == {"cases", "by_capability"}, (
        "no overall pass rate: an aggregate over exact RPCs and heuristic joins cannot be acted on"
    )
    assert summary["by_capability"]["discovery"] == {
        "cases": 2,
        "passed": 1,
        "rate": 0.5,
        "failed_checks": {"names_expected_relation": 1},
        "failed": ["b"],
    }
    assert summary["by_capability"]["find_district_rpc"]["passed"] == 0
    assert "verified_something" in summary["by_capability"]["find_district_rpc"]["failed_checks"]


def test_summary_names_which_check_failed_most() -> None:
    """A failure mode, not a case list. Which check is broken says where the work is."""
    results = [grade.grade_case(case(id=str(i), expected={"relation": "x.y"}), run("")) for i in range(3)]
    summary = grade.summarise(results)
    assert summary["by_capability"]["discovery"]["failed_checks"] == {"names_expected_relation": 3}


# --------------------------------------------------------------------------------------------
# The real suite must be loadable and fully gradable.


def test_the_generated_suite_has_no_capability_the_grader_cannot_grade() -> None:
    """Every generated capability must have a grader, or the suite grades as holes.

    This is the contract that catches a new capability being generated before the grader knows it -
    which would otherwise show up as a silently failing case rather than an error.
    """
    path = ROOT / "eval" / "generated" / "questions.json"
    if not path.is_file():
        pytest.skip("the generated suite is absent")
    cases = grade.load_cases(str(path))
    known = set(grade._GRADERS)
    unknown = sorted(
        {str(c.get("capability")) for c in cases}
        - known
        - {"multi_step_join"}
        - {c for c in {str(x.get("capability")) for x in cases} if "join" in c}
    )
    assert not unknown, f"capabilities with no grader: {unknown}; add one rather than letting them score as holes"


def test_every_generated_case_carries_something_to_grade() -> None:
    """`expected` cannot be empty: a case with nothing to check is decoration."""
    path = ROOT / "eval" / "generated" / "questions.json"
    if not path.is_file():
        pytest.skip("the generated suite is absent")
    empty = [c.get("id") for c in grade.load_cases(str(path)) if not (c.get("expected") or c.get("forbidden_claims"))]
    assert not empty, f"cases with neither expected values nor forbidden claims: {empty}"


def test_answer_text_includes_reasoning_as_well_as_the_final_answer() -> None:
    """A model can assert in prose something it never verified; the grader has to be able to see it."""
    run_result = {"answer": "42", "turns": [{"turn": 1, "reasoning": "I did an unsigned join to get there."}]}
    text = grade.answer_text(run_result)
    assert "42" in text and "unsigned join" in text
