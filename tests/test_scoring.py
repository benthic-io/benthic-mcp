"""The holdout-split mechanics of run_eval.

The strict scorer that used to live in this file was retired when run_eval was reconciled with the
truth grader (grade.py): driving the model through the real MCP server is run_eval's job, and
deciding whether the run was correct is the grader's, so one correctness instrument serves both
driving paths. The retired checks' ports - server result and route checks - are contracted in
tests/test_truth_grader.py under their new names.

"""


def test_the_holdout_split_covers_several_capabilities() -> None:
    # A plain hash drew five easy cases into the holdout and the tripwire could not fail.
    from run_eval import select_cases

    cases = [
        {"id": "discovery_a", "capability": "discovery"},
        {"id": "discovery_b", "capability": "discovery"},
        {"id": "join_a", "capability": "identifier_reliable_join"},
        {"id": "join_b", "capability": "identifier_reliable_join"},
        {"id": "multi_a", "capability": "multi_step_join"},
        {"id": "multi_b", "capability": "multi_step_join"},
        {"id": "trap_a", "capability": "relation_trap"},
        {"id": "trap_b", "capability": "relation_trap"},
    ]

    holdout = select_cases(cases, None, None, "holdout")

    # Every capability with at least two cases contributes one holdout case.
    assert {case["capability"] for case in holdout} == {
        "discovery",
        "identifier_reliable_join",
        "multi_step_join",
        "relation_trap",
    }


def test_the_holdout_excludes_cases_when_a_capability_has_only_one() -> None:
    from run_eval import select_cases

    cases = [
        {"id": "solo", "capability": "lonely"},
        {"id": "a", "capability": "paired"},
        {"id": "b", "capability": "paired"},
    ]

    holdout = {case["id"] for case in select_cases(cases, None, None, "holdout")}

    assert "solo" not in holdout
    assert len(holdout) == 1


def test_holdout_membership_survives_case_filtering() -> None:
    """The holdout is a property of the suite, so a --case run must report the same membership.

    Deriving it from the filtered list instead meant a narrow run had no holdout at all, and since
    --split defaults to "all" that is exactly when the harness needed to know.
    """
    from run_eval import assign_splits, select_cases

    cases = [
        {"id": "discovery_a", "capability": "discovery"},
        {"id": "discovery_b", "capability": "discovery"},
        {"id": "join_a", "capability": "identifier_reliable_join"},
        {"id": "join_b", "capability": "identifier_reliable_join"},
    ]
    suite_holdout = assign_splits(cases)

    filtered = select_cases(cases, "discovery_", None, "all", suite_holdout)
    holdout = select_cases(cases, "discovery_", None, "holdout", suite_holdout)

    # discovery_a is held out suite-wide, so filtering to discovery must still surface it as holdout.
    assert suite_holdout == {"discovery_a", "join_a"}
    assert {case["id"] for case in filtered} == {"discovery_a", "discovery_b"}
    assert {case["id"] for case in holdout} == {"discovery_a"}


def test_a_batch_with_no_recorded_holdout_falls_back_to_computing_one() -> None:
    from run_eval import select_cases

    cases = [{"id": "a", "capability": "paired"}, {"id": "b", "capability": "paired"}]

    assert {case["id"] for case in select_cases(cases, None, None, "holdout")} == {"a"}
