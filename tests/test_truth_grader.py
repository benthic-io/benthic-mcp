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
import json
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


def run(
    answer: str,
    reasoning: str = "",
    tools: list[ToolCall] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a case_run in the shape sweep.py writes. `payload` is the structured result of every tool."""
    results = [
        {
            "name": name,
            "args": args,
            "ok": ok,
            "text": json.dumps(payload, sort_keys=True) if payload is not None else "{}",
        }
        for name, args, ok in (tools or [])
    ]
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
        run("a.b has x.", tools=[("benthic_discover", {}, True)], payload={"relations": [{"source": "a.b"}]}),
    )
    assert not result.passed
    assert failing(result) == ["names_column:y"]


def test_a_case_the_harness_could_not_run_is_not_a_capability_failure() -> None:
    """A 500 is not the model failing, and averaging it into a rate makes the model look worse.

    The first run reported relation_trap 0/2. One case was an HTTP 500 with zero turns and the other
    an 8,590-character reasoning turn cut off by the token budget. Neither measured the capability.
    """
    errored = grade.grade_case(
        case(capability="relation_trap", expected={"relation": "a.b"}),
        {"answer": "", "turns": [], "error": "HTTPStatusError: 500 Internal Server Error"},
    )
    assert not errored.passed
    assert failing(errored) == ["errored"]
    check = next(c for c in errored.checks if c.name == "errored")
    assert "not a model failure" in check.detail


def test_a_turn_truncated_by_the_token_budget_is_not_a_capability_failure() -> None:
    truncated = grade.grade_case(
        case(capability="relation_trap", expected={"relation": "a.b"}),
        {"answer": "", "turns": [{"turn": 1, "reasoning": "x" * 8590, "finish_reason": "length"}]},
    )
    assert not truncated.passed
    assert failing(truncated) == ["truncated"]


def test_a_clean_finish_is_not_reported_as_truncated() -> None:
    ok = grade.grade_case(
        case(capability="relation_trap", expected={"relation": "a.b"}),
        {"answer": "a.b holds it", "turns": [{"turn": 1, "reasoning": "r", "finish_reason": "stop"}]},
    )
    assert "truncated" not in failing(ok)


def test_the_summary_counts_a_capability_rate_over_measured_cases_only() -> None:
    """An unmeasurable case must not drag a capability's rate down."""

    def graded(case_id: str) -> dict[str, Any]:
        return case(
            id=case_id,
            capability="discovery",
            required_tools=["discover"],
            expected={"relation": "a.b"},
        )

    def ok_run() -> dict[str, Any]:
        return run("a.b holds it", tools=[("benthic_discover", {}, True)], payload={"relations": [{"source": "a.b"}]})

    results = [
        grade.grade_case(graded("a"), ok_run()),
        grade.grade_case(graded("b"), ok_run()),
        grade.grade_case(graded("c"), {"answer": "", "turns": [], "error": "500"}),
    ]
    bucket = grade.summarise(results)["by_capability"]["discovery"]
    assert bucket["not_measured"] == 1
    assert bucket["measured"] == 2
    assert bucket["rate"] == 1.0, "two passing measured cases is 100%, not 67%"


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
    result = grade.grade_case(case(), run(answer), {"usaspending.prime_awards"}, COLUMNS)
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
        COLUMNS,
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
        run(
            "1 row",
            tools=[("benthic_rpc", dict(BBOX, operation="districts_in_bbox"), True)],
            payload={"row_count": 1, "rows": [{}]},
        ),
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


def trap_case() -> dict[str, Any]:
    return case(
        capability="relation_trap",
        required_tools=["query"],
        expected={"relation": "usp_cl.legislator_terms", "trap": "usp_cl.mv_current_lawmakers"},
    )


def trap_result(answer: str) -> Any:
    return grade.grade_case(
        trap_case(), run(answer, tools=[("benthic_query", {"source": "usp_cl.legislator_terms"}, True)])
    )


def test_naming_the_trap_in_order_to_reject_it_is_not_relying_on_it() -> None:
    """A run graded this failed where the model wrote "NOT mv_current_lawmakers which is current-only".

    It named the trap precisely so it could set it aside, and said why. Grading that as relying on the
    trap punishes the model for being explicit about its reasoning, which is the opposite of what this
    check is for.
    """
    result = trap_result(
        "I used usp_cl.legislator_terms (NOT usp_cl.mv_current_lawmakers which is current-only) for the 117th Congress."
    )
    assert result.passed, [c.name for c in result.failures()]
    check = next(c for c in result.checks if c.name == "did_not_answer_from_trap")
    assert check.found == "named and rejected"


def test_answering_from_the_trap_still_fails() -> None:
    result = trap_result("Answering from usp_cl.mv_current_lawmakers gives the sitting member.")
    assert not result.passed
    assert "did_not_answer_from_trap" in failing(result)
    check = next(c for c in result.failures() if c.name == "did_not_answer_from_trap")
    assert check.found == "relied on"


def test_one_rejection_does_not_launder_a_later_reliance() -> None:
    """Both mentions must be rejected. A model that explains the trap and then uses it has used it."""
    result = trap_result(
        "From usp_cl.legislator_terms I avoided usp_cl.mv_current_lawmakers because it is "
        "current-only. Reading from usp_cl.mv_current_lawmakers gives the answer."
    )
    assert "used_expected_relation" not in failing(result), (
        "the test is about the trap check, so the expected relation must be present"
    )
    assert "did_not_answer_from_trap" in failing(result)


def test_a_rejection_far_from_the_mention_is_not_a_rejection() -> None:
    """A window, not the whole answer: 'not X' far from X is not a rejection of it."""
    padding = "the record is historical. " * 40
    result = trap_result("From usp_cl.legislator_terms: " + padding + "Reading usp_cl.mv_current_lawmakers gives it.")
    assert "did_not_answer_from_trap" in failing(result)


def test_the_trap_check_misses_a_rejection_it_cannot_see_and_says_so() -> None:
    """The known limit, asserted rather than left implicit.

    A rejection placed with no marker inside the sentence - "I avoided X", where the only negation is
    the verb - is not detectable by a marker scan, and widening the marker list to catch it would
    admit phrasings where the model *did* rely on the trap. A missed case is recoverable; a false pass
    is not, so the limit is documented where the check is instead of engineered away.
    """
    import inspect

    assert (
        grade._trap_is_rejected("I avoided usp_cl.mv_current_lawmakers entirely.", "usp_cl.mv_current_lawmakers")
        is False
    )
    source = inspect.getsource(grade.check_relation_trap)
    assert "weaker" in source and "would still pass" in source


def test_the_trap_check_states_that_it_is_weaker_than_reading_the_answer() -> None:
    """Its limitation is documented where the check is, so a reader knows what it does not catch."""
    import inspect

    source = inspect.getsource(grade.check_relation_trap)
    assert "would still pass" in source, "the known weakness must be stated in the function"


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
        grade.grade_case(
            case(id="a", capability="discovery", expected={"relation": "x.y"}),
            run("x.y", tools=[("benthic_discover", {}, True)], payload={"relations": [{"source": "x.y"}]}),
        ),
        grade.grade_case(case(id="b", capability="discovery", expected={"relation": "x.y"}), run("")),
        grade.grade_case(case(id="c", capability="find_district_rpc", expected={}), run("")),
    ]
    summary = grade.summarise(results)
    assert set(summary) == {"cases", "by_capability"}, (
        "no overall pass rate: an aggregate over exact RPCs and heuristic joins cannot be acted on"
    )
    discovery = summary["by_capability"]["discovery"]
    # Asserted field by field rather than as one dict literal, so adding a field to the summary does
    # not read as a behavioural regression. The values are the contract.
    assert discovery["cases"] == 2
    assert discovery["passed"] == 1
    assert discovery["rate"] == 0.5
    assert discovery["failed_checks"] == {"names_expected_relation": 1, "expected_relation_retrieved": 1}
    assert discovery["failed"] == ["b"]
    assert discovery["measured"] == 2 and discovery["not_measured"] == 0
    assert summary["by_capability"]["find_district_rpc"]["passed"] == 0
    assert "verified_something" in summary["by_capability"]["find_district_rpc"]["failed_checks"]


def test_summary_names_which_check_failed_most() -> None:
    """A failure mode, not a case list. Which check is broken says where the work is."""
    results = [grade.grade_case(case(id=str(i), expected={"relation": "x.y"}), run("")) for i in range(3)]
    summary = grade.summarise(results)
    assert summary["by_capability"]["discovery"]["failed_checks"] == {
        "names_expected_relation": 3,
        "expected_relation_retrieved": 3,
    }


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


# --------------------------------------------------------------------------------------------
# A count is a number, and a zero-row result is written in words more often than in digits.


def _rpc_case(row_count: int | None) -> dict[str, Any]:
    return {
        "id": "r",
        "capability": "find_district_rpc",
        "question": "q",
        "expected": {"relation": "a.b", "row_count": row_count},
        "forbidden_claims": [],
    }


def _rpc_failing(answer: str, row_count: int | None) -> list[str]:
    checks = grade.check_rpc(_rpc_case(row_count), {"answer": answer})
    return [c.name for c in checks if not c.ok]


@pytest.mark.parametrize(
    "answer",
    [
        "The find_district RPC returned 0 rows.",  # the digit
        "The find_district RPC returned zero rows.",  # the word
        "No district was returned.",  # the result negated
        "The result is empty.",  # the state
        "It returned no rows for that point.",
        "Nothing matched.",
    ],
)
def test_a_count_of_zero_is_stated_in_six_ordinary_ways(answer: str) -> None:
    """`reports_row_count` searched for `str(row_count)` and graded six correct answers as silent.

    A zero-row result is exactly what a point outside a dataset's coverage produces, so the answer
    that most needs checking - "there is nothing here" - was the one that failed. The model wrote
    "returned zero rows" and was marked as not stating a count.
    """
    assert "reports_row_count" not in _rpc_failing(answer, 0), answer


@pytest.mark.parametrize(
    "answer",
    [
        "The find_district RPC returned 3 rows.",
        "It returned one district.",
        "There were 127 matches.",
    ],
)
def test_a_different_count_is_still_a_failure(answer: str) -> None:
    """Reading the count as a number must not admit a wrong one."""
    assert "reports_row_count" in _rpc_failing(answer, 0), answer


def test_a_count_stated_with_a_thousands_separator_is_read() -> None:
    assert "reports_row_count" not in _rpc_failing("The join returned 1,234 rows.", 1234)


def test_the_join_count_check_reads_numbers_the_same_way() -> None:
    """`check_signed_path` and `check_sequential` each had their own copy of the digit search."""
    case = {
        "id": "j",
        "capability": "identifier_partial_join",
        "question": "q",
        "expected": {
            "join_path": {
                "join_type": "identifier",
                "left": "usaspending.all_entities",
                "left_column": "congressional_district",
                "reliability": "partial",
                "right": "usp_cl.legislator_terms",
                "right_column": "district",
            },
            "right_count": 127,
        },
        "forbidden_claims": [],
        "required_tools": ["benthic_join"],
    }
    # The signed-edge check fires on these bare answers, so this asserts only about the count.
    stated = grade.check_signed_path(case, {"answer": "The join returned 127 rows."})
    assert [c.name for c in stated if c.name == "reports:right_count" and not c.ok] == []
    vague = grade.check_signed_path(case, {"answer": "The join returned multiple rows."})
    assert [c.name for c in vague if not c.ok and c.name.startswith("reports:")] == ["reports:right_count"]


# --------------------------------------------------------------------------------------------
# The runner must arm the whole anti-hallucination floor, not half of it.


def _floor_suite() -> Any:
    """The runner's own grading seam, loaded the way run_suite loads the grader."""
    spec = importlib.util.spec_from_file_location("run_suite", ROOT / "eval" / "truth" / "run_suite.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_suite"] = module
    spec.loader.exec_module(module)
    return module


def test_the_runner_grades_with_the_column_names_and_not_only_the_relations() -> None:
    """`manifest_names()` returns 1,488 columns and the runner discarded them.

    `grade_case` takes `column_names` to recognise a column alias - `left.state`, `right.uei` - as
    something a model writes when reporting join evidence rather than an invented relation. The
    runner fetched the columns, bound them to a local, and passed only the relations. So the alias
    protection was inert at run time: it worked whenever a person graded interactively and did
    nothing whenever the runner graded, which is the only way a number gets recorded.

    Rep 2 failed `multi_step_join 0/2` on `unmanifested_relation` for exactly this reason, and the
    same transcripts pass once the columns are supplied.
    """
    run_suite = _floor_suite()
    suite_case = {
        "id": "m",
        "capability": "multi_step_join",
        "question": "q",
        "expected": {"join_path": {"left": "a.b", "left_column": "uei", "right": "c.d", "right_column": "uei"}},
        "forbidden_claims": [],
        "required_tools": ["benthic_join"],
    }
    recorded = {
        "id": "m",
        "answer": "left.state is MD and right.uei is ESELKUJSAM45",
        "turns": [
            {
                "tool_results": [
                    {
                        "name": "benthic_join",
                        "args": {},
                        "ok": True,
                        "text": "{}",
                    }
                ]
            }
        ],
    }
    results = run_suite.grade_runs([recorded], [suite_case], {"a.b", "c.d"}, {"state", "uei", "congressional_district"})
    alias = [c for r in results for c in r.checks if c.name == "unmanifested_relation"]
    assert alias and alias[0].ok, f"the floor rejected a column alias: {[c.found for c in alias]}"


def test_the_floor_still_rejects_an_invented_relation_through_the_runner() -> None:
    """The same seam, so the pass above is the alias check working and not the floor being off."""
    run_suite = _floor_suite()
    suite_case = {
        "id": "m",
        "capability": "multi_step_join",
        "question": "q",
        "expected": {"join_path": {"left": "a.b", "left_column": "uei", "right": "c.d", "right_column": "uei"}},
        "forbidden_claims": [],
        "required_tools": ["benthic_join"],
    }
    recorded = {
        "id": "m",
        "answer": "I read usaspending.invented_table and samer.ghost_relation",
        "turns": [{"tool_results": [{"name": "benthic_join", "args": {}, "ok": True, "text": "{}"}]}],
    }
    results = run_suite.grade_runs([recorded], [suite_case], {"a.b", "c.d"}, {"state", "uei"})
    floor = [c for r in results for c in r.checks if c.name == "unmanifested_relation"]
    assert floor and not floor[0].ok, floor


def test_the_floor_ignores_the_syntax_vocabulary_the_docs_advertise() -> None:
    """`column=operator.value` and `order=column.desc.nullslast` are syntax templates, not relations.

    A model quoting them in reasoning is not inventing a table, but the floor reported
    `operator.value` as invented across every copy of the fan-out case - three transcripts failed
    `unmanifested_relation` for a relation nobody named, which converts a real floor into noise.
    """
    run_suite = _floor_suite()
    suite_case = {
        "id": "m",
        "capability": "multi_step_join",
        "question": "q",
        "expected": {"join_path": {"left": "a.b", "left_column": "uei", "right": "c.d", "right_column": "uei"}},
        "forbidden_claims": [],
        "required_tools": ["benthic_join"],
    }
    recorded = {
        "id": "m",
        "answer": "the join returned 127 rows",
        "turns": [
            {
                "reasoning": (
                    'The format is "column=operator.value" for filters and "order=column.desc.nullslast" for order.'
                ),
                "tool_results": [{"name": "benthic_join", "args": {}, "ok": True, "text": "{}"}],
            }
        ],
    }
    results = run_suite.grade_runs([recorded], [suite_case], {"a.b", "c.d"}, {"state", "uei"})
    floor = [c for r in results for c in r.checks if c.name == "unmanifested_relation"]
    assert floor and floor[0].ok, f"the floor rejected syntax vocabulary: {[c.found for c in floor]}"


# --------------------------------------------------------------------------------------------
# A case must ask for what its check tests, or the measurement is unattainable.


def test_a_relation_trap_case_asks_about_the_trap_and_not_for_a_large_enumeration() -> None:
    """The case asked for a chronological list of officeholders with term boundaries.

    That is a large answer, and it is what broke the measurement: the truncated turn's reasoning grew
    335 -> 1,838 -> 29,672 chars while the model worked through what it was not looking at. Meanwhile
    the case's own check tests exactly one thing - that the model names `legislator_terms` and rejects
    `mv_current_lawmakers`. The enumeration is incidental to that, and it is what made the capability
    unmeasurable in four parameterisations.

    A question that cannot be answered inside the model's practical budget does not measure the
    capability. This asserts the question asks the capability question and leaves the row-listing to
    cases that measure listing.
    """
    path = ROOT / "eval" / "generated" / "questions.json"
    if not path.is_file():
        pytest.skip("the generated suite is absent")
    for case in grade.load_cases(str(path)):
        if case.get("capability") != "relation_trap":
            continue
        question = str(case.get("question") or "").lower()
        # The question must not hand the model the relation, or `used_expected_relation` is testing
        # whether it can copy a name rather than whether it can find the historical record.
        assert "legislator_terms" not in question, f"{case['id']} gives away the expected relation"
        # The trap must be named, or a model cannot be told what to avoid.
        assert "view" in question or "present-day" in question, case["id"]
        # No instruction to enumerate a set of rows, which is what outruns the budget.
        for phrase in ("in chronological order", "list the officeholders", "who held the office"):
            assert phrase not in question, f"{case['id']} asks for an enumeration: {phrase!r}"


def test_the_relation_trap_check_still_tests_rejection_rather_than_selection() -> None:
    """So shortening the question did not also weaken the check it feeds."""
    trap_case = {
        "id": "t",
        "capability": "relation_trap",
        "question": "q",
        "expected": {"relation": "usp_cl.legislator_terms", "trap": "usp_cl.mv_current_lawmakers"},
        "forbidden_claims": [],
        "required_tools": ["benthic_query"],
    }
    relies = grade.grade_case(trap_case, {"answer": "I used usp_cl.mv_current_lawmakers for this."})
    assert not relies.passed, [c.name for c in relies.checks]
    # The rejection lands after the mention, which is the phrasing the window used to miss.
    rejects = grade.grade_case(
        trap_case,
        {
            "answer": (
                "usp_cl.legislator_terms is the source. usp_cl.mv_current_lawmakers holds only "
                "present-day rows, so it cannot answer a question about the 117th Congress."
            ),
            "turns": [
                {
                    "turn": 1,
                    "reasoning": "",
                    "tool_results": [
                        {
                            "name": "benthic_query",
                            "args": {"source": "usp_cl.legislator_terms"},
                            "ok": True,
                            "text": "{}",
                        }
                    ],
                }
            ],
        },
    )
    assert [c.name for c in rejects.checks if not c.ok] == [], [c.name for c in rejects.checks if not c.ok]


# --------------------------------------------------------------------------------------------
# A rejection after the mention is the more natural phrasing, and it must count.


TRAP = "usp_cl.mv_current_lawmakers"


@pytest.mark.parametrize(
    "answer",
    [
        "not usp_cl.mv_current_lawmakers, because it is current-only",  # rejection first
        "usp_cl.mv_current_lawmakers is current-only and would be wrong",  # rejection after
        "Read from legislator_terms. usp_cl.mv_current_lawmakers holds only present-day rows, so it cannot answer this",
        "The present-day view usp_cl.mv_current_lawmakers does not answer a historical question",
        "I used usp_cl.mv_current_lawmakers earlier but it is current-only, so legislator_terms is the right source",
    ],
)
def test_a_rejection_counts_wherever_the_model_puts_it(answer: str) -> None:
    """`_trap_is_rejected` detected sentence boundaries with `find(".")`, and the trap name is
    `dataset.relation`, so the period inside the identifier ended the window inside the trap's own
    name. A rejection placed after the mention was never in the window, and the model was graded as
    relying on the thing it had just explained it could not use.

    The comment above that function claims both directions count. The code did not implement it, which
    is worse than the defect - it stops the next reader from checking.
    """
    assert grade._trap_is_rejected(answer, TRAP), answer


@pytest.mark.parametrize(
    "answer",
    [
        "I used usp_cl.mv_current_lawmakers for the 117th Congress.",
        "usp_cl.mv_current_lawmakers is the fastest source for this.",
    ],
)
def test_a_genuine_reliance_still_fails(answer: str) -> None:
    """So the window widening did not simply accept everything."""
    assert not grade._trap_is_rejected(answer, TRAP), answer


def test_a_rejection_does_not_launder_a_later_reliance() -> None:
    assert not grade._trap_is_rejected(
        "I avoided mv_current_lawmakers because it is current-only. Reading from "
        "usp_cl.mv_current_lawmakers gives the answer",
        TRAP,
    )


# --------------------------------------------------------------------------------------------
# A rejection may spill into the sentence after the mention.


@pytest.mark.parametrize(
    "answer",
    [
        # the real transcript: the sentence ends before the reason starts
        "usp_cl.mv_current_lawmakers is a materialized view that returns only the currently sitting "
        "member(s). it has no historical timeline, so it cannot tell you who held the seat in 2021",
        "usp_cl.mv_current_lawmakers is a materialized view. It holds no historical timeline.",
    ],
)
def test_a_rejection_in_the_following_sentence_counts(answer: str) -> None:
    """The window closed at the end of the mention's own sentence.

    A model explaining why a view is wrong routinely states the property in one sentence and the
    consequence in the next. Case 0_0 was graded `relied on` while case 0_1 passed, and the difference
    was one sentence break inside the same explanation.
    """
    assert grade._trap_is_rejected(answer, TRAP), answer


def test_widening_by_one_sentence_still_catches_a_launder() -> None:
    """The window exists to stop one rejection from covering a second, genuine reliance."""
    assert not grade._trap_is_rejected(
        "usp_cl.mv_current_lawmakers cannot answer a historical question. Reading from "
        "usp_cl.mv_current_lawmakers gives the answer for the 117th Congress anyway.",
        TRAP,
    )


# --------------------------------------------------------------------------------------------
# The anti-hallucination floor must say when it is only half armed.


# What the runner passes: 1,488 real manifest column names. A floor test that passes
# relations only cannot tell a column alias from an invented relation, which is how the
# half-arming defect survived three runs.
COLUMNS = {"prime_awards", "total_obligation", "uei", "state", "congressional_district"}


def _floor_case(**expected: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"relation": "a.b", "row_count": 0}
    base.update(expected)
    return {"id": "f", "capability": "find_district_rpc", "question": "q", "expected": base, "forbidden_claims": []}


def test_a_half_armed_floor_reports_itself_rather_than_failing_a_column_alias() -> None:
    """`signed_relations` without `column_names` silently disabled the alias check.

    The runner did exactly that for three runs, so `left.state` and `right.uei` were graded as invented
    relations and `multi_step_join 0/2` was an artefact. The asymmetry is the defect: `None` means
    "not supplied", and the floor then cannot tell a column alias from an invented relation, and says
    nothing about it.

    Every instrument reports its own coverage. The floor now does.
    """
    relations = {"a.b", "c.d"}
    run = {"answer": "left.state is MD"}
    half = grade.grade_case(_floor_case(), run, relations)
    coverage = [c for c in half.checks if c.name == "manifest_floor_coverage"]
    assert coverage, [c.name for c in half.checks]
    assert not coverage[0].ok, "a half-armed floor must not report itself as armed"
    assert "column alias" in coverage[0].detail, coverage[0].detail


def test_a_fully_armed_floor_reports_itself_as_armed() -> None:
    from_check = grade.grade_case(_floor_case(), {"answer": "zero rows"}, {"a.b", "c.d"}, {"state", "uei"})
    coverage = [c for c in from_check.checks if c.name == "manifest_floor_coverage"]
    assert coverage, [c.name for c in from_check.checks]
    assert coverage[0].ok
    assert coverage[0].found == "relations and columns"


def test_an_unarmed_floor_stays_silent() -> None:
    """Passing neither is the documented behaviour, not a new failure mode.

    Callers that grade by hand with no manifest at all should not acquire a failing check for it.
    """
    checks = grade.grade_case(_floor_case(), {"answer": "zero rows"}).checks
    assert [c.name for c in checks if c.name == "manifest_floor_coverage"] == []


# --------------------------------------------------------------------------------------------
# The tool-trace family. The canary's model-free server-bug detection reads the tool RESULTS,
# not the model's prose: a server that returns the wrong row count must flip a check even when
# the model faithfully reports what it was given. These checks were the legacy scorer's, ported
# into the one grader so an end-to-end run and a direct run grade alike.


def _result_run(answer: str, tools: list[tuple[str, dict[str, Any], bool, dict[str, Any]]]) -> dict[str, Any]:
    return {
        "answer": answer,
        "turns": [
            {
                "turn": 1,
                "reasoning": "",
                "tool_results": [
                    {"name": name, "args": args, "ok": ok, "text": json.dumps(payload, sort_keys=True)}
                    for name, args, ok, payload in tools
                ],
            }
        ],
    }


def _check(results: list[Any], name: str) -> Any:
    return next((c for r in results for c in r.checks if c.name == name), None)


JOIN_CASE = {
    "id": "j",
    "capability": "identifier_partial_join",
    "question": "q",
    "expected": {
        "join_path": {
            "left": "usaspending.all_entities",
            "left_column": "congressional_district",
            "right": "usp_cl.legislator_terms",
            "right_column": "district",
            "reliability": "partial",
        },
        "right_count": 127,
    },
    "forbidden_claims": [],
    "required_tools": ["benthic_join"],
}

JOIN_ARGS = {
    "left_source": "usaspending.all_entities",
    "right_source": "usp_cl.legislator_terms",
    "left_column": "congressional_district",
    "right_column": "district",
}


def test_the_join_result_right_count_is_read_from_the_server_result() -> None:
    """A server returning 127 rows must satisfy right_count: 127; one returning 0 must not."""
    joined = _result_run(
        "The join returned 127 rows.",
        [
            (
                "benthic_join",
                JOIN_ARGS,
                True,
                {"row_count": 127, "rows": [{"right.district": 3}] * 127, "truncated": False},
            )
        ],
    )
    empty = _result_run(
        "The join returned nothing.",
        [("benthic_join", JOIN_ARGS, True, {"row_count": 0, "rows": [], "truncated": False})],
    )

    ok = _check([grade.grade_case(JOIN_CASE, joined)], "server_right_count")
    assert ok is not None and ok.ok, "the server's 127 rows must satisfy right_count: 127"
    bad = _check([grade.grade_case(JOIN_CASE, empty)], "server_right_count")
    assert bad is not None and not bad.ok, "the server's 0 rows must fail right_count: 127"


def test_a_zero_right_count_requires_every_join_call_to_come_back_empty() -> None:
    """A zero expectation guards a too-wide join, so one empty call must not launder a matched one."""
    zero_case = {**JOIN_CASE, "expected": {**JOIN_CASE["expected"], "right_count": 0}}
    clean = _result_run(
        "Nothing joined.", [("benthic_join", JOIN_ARGS, True, {"row_count": 0, "rows": [], "truncated": False})]
    )
    leaked = _result_run(
        "Nothing joined.",
        [
            ("benthic_join", JOIN_ARGS, True, {"row_count": 0, "rows": [], "truncated": False}),
            (
                "benthic_join",
                JOIN_ARGS,
                True,
                {"row_count": 3, "rows": [{"right.district": 3}] * 3, "truncated": False},
            ),
        ],
    )

    ok = _check([grade.grade_case(zero_case, clean)], "server_right_count")
    assert ok is not None and ok.ok
    bad = _check([grade.grade_case(zero_case, leaked)], "server_right_count")
    assert bad is not None and not bad.ok, "one empty call must not carry a call that matched rows"


def test_the_right_count_tolerates_a_filled_limit_when_the_result_is_truncated() -> None:
    """A bounded query that fills its limit answered as far as it was asked to."""
    case_127 = {**JOIN_CASE, "expected": {**JOIN_CASE["expected"], "right_count": 1000}}
    filled = _result_run(
        "The join returned 100 rows of more.",
        [
            (
                "benthic_join",
                {**JOIN_ARGS, "limit": 100},
                True,
                {"row_count": 100, "rows": [{}] * 100, "truncated": True},
            )
        ],
    )
    ok = _check([grade.grade_case(case_127, filled)], "server_right_count")
    assert ok is not None and ok.ok, "a truncated result at the caller's own limit is not a server defect"


def test_the_join_result_right_keys_are_compared_as_distinct_values() -> None:
    """right_keys_distinct is a set of distinct right-hand values, not a per-row list."""
    key_case = {
        **JOIN_CASE,
        "expected": {
            **JOIN_CASE["expected"],
            "join_path": {**JOIN_CASE["expected"]["join_path"], "right_column": "uei"},
            "right_keys_distinct": ["A", "B"],
        },
    }
    rows = [{"right.uei": "A"}, {"right.uei": "B"}, {"right.uei": "B"}]
    good = _result_run(
        "A and B joined.", [("benthic_join", JOIN_ARGS, True, {"row_count": 3, "rows": rows, "truncated": False})]
    )
    missing = _result_run(
        "Only A joined.",
        [("benthic_join", JOIN_ARGS, True, {"row_count": 1, "rows": [{"right.uei": "A"}], "truncated": False})],
    )

    ok = _check([grade.grade_case(key_case, good)], "server_right_keys")
    assert ok is not None and ok.ok
    bad = _check([grade.grade_case(key_case, missing)], "server_right_keys")
    assert bad is not None and not bad.ok, "a missing distinct key value is a wrong result"


def test_the_join_reliability_is_read_from_the_server_result() -> None:
    """The signed edge's reliability is a field on the result, not a phrase in the answer."""
    true_result = _result_run(
        "Partial join.",
        [("benthic_join", JOIN_ARGS, True, {"row_count": 127, "joins": [{"reliability": "partial"}]})],
    )
    wrong_result = _result_run(
        "Partial join.",
        [("benthic_join", JOIN_ARGS, True, {"row_count": 127, "joins": [{"reliability": "reliable"}]})],
    )

    ok = _check([grade.grade_case(JOIN_CASE, true_result)], "server_join_reliability")
    assert ok is not None and ok.ok
    bad = _check([grade.grade_case(JOIN_CASE, wrong_result)], "server_join_reliability")
    assert bad is not None and not bad.ok, "a result reporting the wrong reliability is a wrong result"


RPC_CASE = {
    "id": "r",
    "capability": "districts_in_bbox_rpc",
    "question": "q",
    "expected": {"operation": "districts_in_bbox", "arguments": {"min_lat": 38.9}, "row_count": 1},
    "forbidden_claims": [],
    "required_tools": ["benthic_rpc"],
}


def test_the_rpc_row_count_is_read_from_the_server_result() -> None:
    """The RPC computes the answer server-side, so the result's row_count is the check."""
    one = _result_run(
        "One district.",
        [
            (
                "benthic_rpc",
                {"operation": "districts_in_bbox", "min_lat": 38.9},
                True,
                {"row_count": 1, "rows": [{"district": 3}]},
            )
        ],
    )
    none = _result_run(
        "No districts.",
        [("benthic_rpc", {"operation": "districts_in_bbox", "min_lat": 38.9}, True, {"row_count": 0, "rows": []})],
    )

    ok = _check([grade.grade_case(RPC_CASE, one)], "rpc_server_row_count")
    assert ok is not None and ok.ok
    bad = _check([grade.grade_case(RPC_CASE, none)], "rpc_server_row_count")
    assert bad is not None and not bad.ok, "the server's 0 rows must fail row_count: 1"


def test_a_zero_rpc_expectation_asserts_no_call_invented_rows() -> None:
    """A zero expectation guards an RPC that invents rows, so every answering call must be empty."""
    zero_case = {**RPC_CASE, "expected": {**RPC_CASE["expected"], "row_count": 0}}
    invented = _result_run(
        "No districts.",
        [
            (
                "benthic_rpc",
                {"operation": "districts_in_bbox", "min_lat": 38.9},
                True,
                {"row_count": 2, "rows": [{"district": 3}, {"district": 4}]},
            )
        ],
    )
    bad = _check([grade.grade_case(zero_case, invented)], "rpc_server_row_count")
    assert bad is not None and not bad.ok, "a call returning rows must fail a zero expectation"


# --------------------------------------------------------------------------------------------
# The rest of the tool-trace family: route checks the legacy scorer carried and the truth
# grader did not.


def test_the_discover_result_must_actually_carry_the_expected_relation() -> None:
    """Naming the relation is not retrieving it; the discover result is the evidence."""
    discovery_case = {
        "id": "d",
        "capability": "discovery",
        "question": "q",
        "expected": {"relation": "irs_ng.bmf_organizations", "columns": ["ein"]},
        "forbidden_claims": [],
        "required_tools": ["benthic_discover"],
    }
    found = _result_run(
        "The relation is irs_ng.bmf_organizations.",
        [("benthic_discover", {"query": "x"}, True, {"relations": [{"source": "irs_ng.bmf_organizations"}]})],
    )
    not_found = _result_run(
        "The relation is irs_ng.bmf_organizations.",
        [("benthic_discover", {"query": "x"}, True, {"relations": [{"source": "samer.sam_registrations"}]})],
    )

    ok = _check([grade.grade_case(discovery_case, found)], "expected_relation_retrieved")
    assert ok is not None and ok.ok
    bad = _check([grade.grade_case(discovery_case, not_found)], "expected_relation_retrieved")
    assert bad is not None and not bad.ok, "an answer claiming a relation discovery never returned must fail"


def test_the_trap_requires_the_historical_relation_to_have_been_queried_when_it_asserts_a_result() -> None:
    """The answer can name the historical relation while the query went to the trap.

    The check applies only where the case asserts something about the result. A trap case that
    asks which relation the caller WOULD read needs no query, and the two stored main-suite cases
    that ask exactly that never query - they were correct as recorded.
    """
    trap_case_dict = {
        "id": "t",
        "capability": "relation_trap",
        "question": "q",
        "expected": {"relation": "usp_cl.legislator_terms", "trap": "usp_cl.mv_current_lawmakers", "right_count": 3},
        "forbidden_claims": [],
        "required_tools": ["benthic_query"],
    }
    historical = _result_run(
        "The answer is in usp_cl.legislator_terms.",
        [("benthic_query", {"source": "usp_cl.legislator_terms"}, True, {"row_count": 3})],
    )
    trapped = _result_run(
        "The answer is in usp_cl.legislator_terms.",
        [("benthic_query", {"source": "usp_cl.mv_current_lawmakers"}, True, {"row_count": 3})],
    )

    ok = _check([grade.grade_case(trap_case_dict, historical)], "historical_relation_queried")
    assert ok is not None and ok.ok
    bad = _check([grade.grade_case(trap_case_dict, trapped)], "historical_relation_queried")
    assert bad is not None and not bad.ok, "querying only the trap must fail, whatever the prose says"


def test_a_trap_case_that_asks_which_relation_to_read_emits_no_query_check() -> None:
    """The false positive this port would have shipped if the gate were left off.

    The legacy scorer demanded the historical relation be queried, failed both stored main-suite trap
    cases for it, and the truth grader passed them correctly. Re-grading the stored run after the port
    with the gate off showed 30/30 and zero shifts, which is what the stored transcripts actually earned.
    """
    trap_case_dict = {
        "id": "t",
        "capability": "relation_trap",
        "question": "q",
        "expected": {"relation": "usp_cl.legislator_terms", "trap": "usp_cl.mv_current_lawmakers"},
        "forbidden_claims": [],
        "required_tools": ["benthic_query"],
    }
    named_only = _result_run("Read usp_cl.legislator_terms.", [("benthic_discover", {"dataset": "usp_cl"}, True, {})])

    verdict = grade.grade_case(trap_case_dict, named_only, None, None)
    assert [check.name for check in verdict.checks if check.name == "historical_relation_queried"] == []


def test_sequential_lookup_requires_both_sources_to_have_been_queried() -> None:
    sequential_case = {
        "id": "s",
        "capability": "sequential_lookup",
        "question": "q",
        "expected": {"left": "usaspending.all_entities", "right": "samer.sam_registrations"},
        "forbidden_claims": [],
        "required_tools": ["benthic_query"],
    }
    both = _result_run(
        "Both lookups done.",
        [
            ("benthic_query", {"source": "usaspending.all_entities"}, True, {"row_count": 1}),
            ("benthic_query", {"source": "samer.sam_registrations"}, True, {"row_count": 1}),
        ],
    )
    one = _result_run(
        "One lookup done.",
        [("benthic_query", {"source": "usaspending.all_entities"}, True, {"row_count": 1})],
    )

    ok = _check([grade.grade_case(sequential_case, both)], "sequential_sources_queried")
    assert ok is not None and ok.ok
    bad = _check([grade.grade_case(sequential_case, one)], "sequential_sources_queried")
    assert bad is not None and not bad.ok, "a sequential lookup that never ran the second source must fail"


EDGE_ONE = {"left": "a.b", "left_column": "uei", "right": "c.d", "right_column": "uei"}
EDGE_TWO = {"left": "c.d", "left_column": "duns", "right": "e.f", "right_column": "duns"}


def _multi_step_case(unanswerable: bool) -> dict[str, Any]:
    return {
        "id": "m",
        "capability": "multi_step_join",
        "question": "q",
        "expected": {"paths": [EDGE_ONE, EDGE_TWO]},
        "forbidden_claims": [],
        "required_tools": ["benthic_join"],
        **({"unanswerable": True} if unanswerable else {}),
    }


def _hop(edge: dict[str, Any], *, reverse: bool = False) -> tuple[str, dict[str, Any], bool, dict[str, Any]]:
    args = (
        {
            "left_source": edge["right"],
            "left_column": edge["right_column"],
            "right_source": edge["left"],
            "right_column": edge["left_column"],
        }
        if reverse
        else {
            "left_source": edge["left"],
            "left_column": edge["left_column"],
            "right_source": edge["right"],
            "right_column": edge["right_column"],
        }
    )
    return ("benthic_join", args, True, {"row_count": 1})


def test_a_two_hop_chain_requires_both_hops_to_have_been_walked() -> None:
    walked = _result_run("Both hops walked.", [_hop(EDGE_ONE), _hop(EDGE_TWO)])
    reversed_hops = _result_run("Both hops walked.", [_hop(EDGE_TWO), _hop(EDGE_ONE, reverse=True)])
    one_hop = _result_run("One hop walked.", [_hop(EDGE_ONE)])

    ok = _check([grade.grade_case(_multi_step_case(False), walked)], "signed_hops_walked")
    assert ok is not None and ok.ok
    reversed_ok = _check([grade.grade_case(_multi_step_case(False), reversed_hops)], "signed_hops_walked")
    assert reversed_ok is not None and reversed_ok.ok, "the direction of a hop is not the hop's identity"
    bad = _check([grade.grade_case(_multi_step_case(False), one_hop)], "signed_hops_walked")
    assert bad is not None and not bad.ok, "one hop of two must fail"


def test_an_unanswerable_chain_requires_the_first_hop_to_have_been_attempted() -> None:
    """The chain is provably empty, so the correct run attempts the first hop and stops."""
    attempted = _result_run("The first hop was attempted and the chain stops there.", [_hop(EDGE_ONE)])
    not_attempted = _result_run("The chain stops there.", [])

    ok = _check([grade.grade_case(_multi_step_case(True), attempted)], "first_hop_attempted")
    assert ok is not None and ok.ok
    bad = _check([grade.grade_case(_multi_step_case(True), not_attempted)], "first_hop_attempted")
    assert bad is not None and not bad.ok, "an unanswerable chain still has to be probed at its first hop"


def test_a_case_with_alternate_routes_requires_one_of_them_to_have_been_walked() -> None:
    """`required_tools_any` accepts either route; walking neither must fail however confident the answer."""
    routes_case = {
        "id": "u",
        "capability": "unsigned_join_rejection",
        "question": "q",
        "expected": {"left": "a.b", "right": "c.d"},
        "forbidden_claims": [],
        "required_tools": [],
        "required_tools_any": [["benthic_discover"], ["benthic_playbook"]],
    }
    by_discover = _result_run("No signed path exists.", [("benthic_discover", {}, True, {"relations": []})])
    by_playbook = _result_run("No signed path exists.", [("benthic_playbook", {}, True, {})])
    by_neither = _result_run("No signed path exists.", [])

    assert _check([grade.grade_case(routes_case, by_discover)], "required_route_used").ok
    assert _check([grade.grade_case(routes_case, by_playbook)], "required_route_used").ok
    bad = _check([grade.grade_case(routes_case, by_neither)], "required_route_used")
    assert bad is not None and not bad.ok, "an assertion of absence with no lookup is a hallucination"


def test_the_rpc_operation_is_read_from_the_call_not_from_the_row_count() -> None:
    """A different RPC returning the same number of rows is not an answer."""
    case_rpc = {
        "id": "r",
        "capability": "districts_in_bbox_rpc",
        "question": "q",
        "expected": {"operation": "districts_in_bbox", "arguments": {"min_lat": 38.9}, "row_count": 1},
        "forbidden_claims": [],
        "required_tools": ["benthic_rpc"],
    }
    right = _result_run(
        "One district.",
        [("benthic_rpc", {"operation": "districts_in_bbox", "min_lat": 38.9}, True, {"row_count": 1})],
    )
    wrong_op = _result_run(
        "One district.",
        [("benthic_rpc", {"operation": "find_district", "min_lat": 38.9}, True, {"row_count": 1})],
    )

    ok = _check([grade.grade_case(case_rpc, right)], "rpc_operation_matches")
    assert ok is not None and ok.ok
    bad = _check([grade.grade_case(case_rpc, wrong_op)], "rpc_operation_matches")
    assert bad is not None and not bad.ok, "a different RPC with the right row count is a different answer"


def test_an_empty_exception_message_still_grades_as_errored_not_as_a_capability_failure() -> None:
    """The hazard, and the fix, in one place.

    `str(httpx.ReadTimeout(""))` is `""`, so an error recorded as a bare `str(exc)` is falsy and the
    grader's `errored` check cannot fire. Measured on the end-to-end run:
    `join_..._usp_cl_legislator_terms_evidence` recorded `error: ''` against a 180s request timeout,
    zero events and an empty answer, and was counted as a model failure with eight failing checks.
    Both drivers now record `f"{type(exc).__name__}: {exc}"`, so the case is reported as `errored`.
    """
    import httpx

    # The shape run_eval's wrapper builds for a case that timed out before any tool call: one turn,
    # no tool results, an empty finish_reason, and whatever `str(exc)` produced.
    def recorded(error: str) -> dict[str, Any]:
        return {"answer": "", "turns": [{"finish_reason": "", "tool_results": []}], "error": error}

    bare = str(httpx.ReadTimeout(""))
    assert not bare, "the precondition: a bare str() of this exception is falsy"
    empty_verdict = grade.grade_case(case(capability="relation_trap", expected={"relation": "a.b"}), recorded(bare))
    assert "errored" not in failing(empty_verdict), (
        "an empty error must not grade as errored: the check cannot see what the harness hit"
    )

    typed = f"{type(httpx.ReadTimeout('')).__name__}: {httpx.ReadTimeout('')}"
    assert typed, "the recorded form is truthy even when the message is empty"
    verdict = grade.grade_case(case(capability="relation_trap", expected={"relation": "a.b"}), recorded(typed))
    assert failing(verdict) == ["errored"], failing(verdict)


def test_both_drivers_record_the_exception_type_with_an_empty_message() -> None:
    """The fix is in the recording, so it is pinned where the recording happens."""
    import httpx

    for path in ("eval/run_eval.py", "eval/truth/run_suite.py"):
        source = (ROOT / path).read_text(encoding="utf-8")
        assert 'f"{type(exc).__name__}: {exc}"' in source, (
            f"{path} records a bare str(exc), so an empty exception message grades as a capability failure"
        )

    exc = httpx.ReadTimeout("")
    assert f"{type(exc).__name__}: {exc}"


def test_the_floor_does_not_report_a_filename_as_an_invented_relation() -> None:
    """`county_01_2009.csv` is a filename, not a table, and the floor reported it as one.

    Measured on the end-to-end run: `sequential_2_usaspending_irs_ng` failed `unmanifested_relation`
    on `county_01_2009.csv`, which the model mentioned in reasoning while describing a census file.
    Both relations it actually named - `usaspending.agency` and `irs_ng.census_demographics` - are
    signed. A floor that reports a filename is a floor that cries wolf, which is how a real floor
    gets ignored.
    """
    relations = {"usaspending.agency", "irs_ng.census_demographics"}
    columns = {"id", "geoid", "county", "year"}
    case_run = {
        "answer": "usaspending.agency and irs_ng.census_demographics answered it.",
        "turns": [
            {
                "turn": 1,
                "reasoning": "The census rows came from county_01_2009.csv.",
                "tool_results": [],
            }
        ],
    }

    result = grade.grade_case(case(capability="sequential_lookup"), case_run, relations, columns)
    assert "unmanifested_relation" not in failing(result), failing(result)


def test_the_floor_still_reports_a_relation_nobody_signed() -> None:
    """The seam, so the filename rule did not arrive by disabling the floor."""
    case_run = {
        "answer": "I read usaspending.invented_table.",
        "turns": [{"turn": 1, "reasoning": "", "tool_results": []}],
    }

    result = grade.grade_case(case(capability="sequential_lookup"), case_run, {"a.b"}, {"x"})
    assert "unmanifested_relation" in failing(result), failing(result)
