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


def test_the_report_separates_a_case_that_was_not_measured_from_one_that_failed() -> None:
    """A harness error is not a wrong answer, and the summary line must not add the two together.

    Measured on the end-to-end run of the 30-case suite: two cases hit the request timeout on a box
    shared with other agents, and `render_report` printed `Failed: 3` - which reads as three cases the
    server or the model got wrong, when two of them were never measured at all. A rate computed over
    that denominator is not a rate of anything. The three numbers have to be reported separately,
    because only two of them describe the server.
    """
    import run_eval

    report = run_eval.render_report(
        [
            {"id": "a", "capability": "c", "events": [], "elapsed_ms": 10, "score": {"passed": True, "errored": False}},
            {
                "id": "b",
                "capability": "c",
                "events": [],
                "elapsed_ms": 10,
                "score": {"passed": False, "unmanifested_relation": True},
            },
            {
                "id": "c",
                "capability": "c",
                "events": [],
                "elapsed_ms": 10,
                "score": {"passed": False, "errored": False},
            },
        ],
        {},
        "run",
    )

    assert "Not measured (harness error): 1" in report, report
    assert "Passed: 1" in report, report
    assert "Failed: 1" in report, report
    assert "Cases: 3" in report, report
    # The wrong-answer failure and the unmeasured case must not be counted as one number.
    assert "Failed: 2" not in report, report
    # And the unmeasured case is named as such rather than listed only under failures.
    failures = report.split("## Failures", 1)[1]
    assert "### c" in failures and "not measured" in failures.lower(), failures

    # `truncated` is the other infrastructure verdict. A turn cut off by the token budget is not a
    # wrong answer either, and on 2026-10-11 one case failed this way and was rendered in the failure
    # table as though the model had got it wrong. `grade.summarise` already counts `truncated` as
    # not-measured, so the two reporters disagreed about the same run. In the score dict the value
    # is the check's verdict, so a failing infrastructure check is `False`, not `True`.
    report = run_eval.render_report(
        [
            {
                "id": "d",
                "capability": "c",
                "events": [],
                "elapsed_ms": 10,
                "score": {"passed": False, "errored": None, "truncated": False},
            }
        ],
        {},
        "run",
    )

    assert "Not measured (harness error): 1" in report, report
    assert "Failed: 0" in report, report
    assert "| d | c | not measured |" in report, report


def test_a_slow_but_progressing_generation_is_not_a_timeout() -> None:
    """A non-streaming read timeout fires on total generation time, not on a stalled stream.

    Measured on the shared llama-server: `join_..._usp_cl_legislator_terms_evidence` started turn 1
    at 31.0 tok/s and collapsed to 0.29 tok/s - about 3.5s per token - the instant a second slot began
    prefilling a 93,587-token prompt. Tokens kept arriving steadily for the next twenty minutes. The
    case failed anyway, because `stream: False` means httpx receives nothing until the whole response
    exists, so the 180s read timeout covers the entire generation rather than the gap between chunks.
    The run reported `ReadTimeout` for a request that was demonstrably still working.

    The distinction that matters: a timeout should mean "nothing is coming", not "this is slow".
    """
    import inspect

    import run_eval

    turn_loop = inspect.getsource(run_eval.run_case)
    assert '"stream": False' not in turn_loop, (
        "a non-streaming request makes the read timeout cover total generation time, so any case "
        "slower than the timeout fails while it is still making progress"
    )
    assert "read_stream(" in turn_loop, "run_case must read completions through the streaming reader"

    reader = inspect.getsource(run_eval.read_stream)
    assert "aiter_lines" in reader or "aiter_bytes" in reader, (
        "the response must be consumed incrementally so the read timeout applies to the gap between "
        "chunks rather than to the whole generation"
    )
