"""The tier-1 canary is only worth running if it can go red.

A canary built from the 33-case suite by picking the fastest cases would be a smaller bill and no
more signal. These contracts assert the property that makes it a canary: every case in it can fail
for a reason the server caused, and the set spans the signed join graph rather than one path of it.

Re-verified against the signed manifest rather than trusted, on the same reasoning as
`test_golden.py`: a canary whose expectations have gone stale is a canary that cannot report a
regression, because the regression and the staleness are indistinguishable.
"""

import json
from pathlib import Path
from typing import Any

CANARY = Path(__file__).resolve().parents[1] / "eval" / "canary" / "questions.json"
GENERATED = Path(__file__).resolve().parents[1] / "eval" / "generated" / "questions.json"


def _cases(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def test_every_canary_case_is_a_copy_of_a_generated_case() -> None:
    """Hand-editing a canary expectation would make it a second, unreviewed answer key."""
    generated = {case["id"]: case for case in _cases(GENERATED)}
    for case in _cases(CANARY):
        assert case["id"] in generated, f"{case['id']} is not in the generated suite"
        assert case == generated[case["id"]], f"{case['id']} was edited; the canary copies, it does not author"


def test_every_canary_case_detects_a_server_bug_in_its_own_direction() -> None:
    """A case that only checks the route measures the model, and cannot detect a server defect.

    27 of the 33 generated cases assert nothing about what the server returned: their `expected`
    blocks carry no row count or key set, so `score_case` hard-codes `answer_ok` to True for them.
    Those cases are worth running once and not worth gating on.

    Each case is fed the wrong answer for the direction it guards, because the directions are not
    interchangeable. A case expecting 0 rows is a guard against a join that is too WIDE and is
    satisfied by a server that returns nothing; a case expecting 127 rows is a guard against one that
    is too NARROW. Asserting that every case catches every wrong answer would be asserting something
    false, and the canary would be unsatisfiable.
    """
    from run_eval import score_case

    for case in _cases(CANARY):
        expected = case["expected"]
        wants_rows = expected.get("right_count") not in (None, 0) or expected.get("row_count") not in (None, 0)
        wrong_count = 0 if wants_rows else 7
        event = {
            "name": "benthic_join" if case["id"].startswith("join_") else "benthic_rpc",
            "ok": True,
            "arguments": _route_arguments(case),
            "structured": {
                "row_count": wrong_count,
                "rows": [{"right.uei": "wrong"}] * wrong_count,
                "truncated": False,
                "joins": [{"reliability": "partial"}],
            },
        }
        score = score_case(case, [event], "Done.", strict=True)
        assert score["answer_check"] is False or score["row_count_check"] is False, (
            f"{case['id']} passes with the server returning {wrong_count} rows, which is the "
            f"regression it is supposed to catch"
        )


def test_the_canary_catches_a_server_that_returns_too_few_rows() -> None:
    """The four zero-expecting cases all pass against a server that returns nothing.

    That is not a flaw in them, it is what a zero expectation means: it guards against a join that
    invents matches. It does mean the canary as a whole is only falsifiable because two of its cases
    expect rows, so the set has to keep at least one. Without this, dropping the district pair would
    leave a canary that reports green against the exact bug it was built to catch.
    """
    from run_eval import score_case

    catching = []
    for case in _cases(CANARY):
        if not case["id"].startswith("join_"):
            continue
        event = {
            "name": "benthic_join",
            "ok": True,
            "arguments": _route_arguments(case),
            "structured": {
                "row_count": 0,
                "rows": [],
                "truncated": False,
                "joins": [{"reliability": "partial"}],
            },
        }
        if score_case(case, [event], "Done.", strict=True)["answer_check"] is False:
            catching.append(case["id"])

    assert catching, (
        "no canary case fails when the server returns nothing, so a server that answers every join "
        "with silence would report green"
    )


def _route_arguments(case: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    if case["id"].startswith("join_"):
        path = expected["join_path"]
        return {
            "left_source": path["left"],
            "right_source": path["right"],
            "left_column": path["left_column"],
            "right_column": path["right_column"],
        }
    return {"operation": expected["operation"], **expected.get("arguments", {})}


def test_the_canary_covers_the_signed_join_graph_rather_than_one_path() -> None:
    """Four of the six cases expect 0 rows, so they catch a join that is too wide, not too narrow.

    Keeping exactly one guard per signed path is deliberate and the asymmetry is worth stating: a
    zero-row expectation is satisfied by a server that returns nothing, so those four cases would
    pass against a join broken the way the district one was. The two cases that expect rows are what
    make the canary falsifiable, which is why dropping them has to fail here.
    """
    cases = _cases(CANARY)
    paths = {
        (
            case["expected"]["join_path"]["left"],
            case["expected"]["join_path"]["right"],
        )
        for case in cases
        if "join_path" in case["expected"]
    }
    assert len(paths) == 4, f"expected one case per signed path, found {len(paths)}: {sorted(paths)}"

    expecting_rows = [
        case
        for case in cases
        if case["expected"].get("right_count") not in (None, 0) or case["expected"].get("row_count") not in (None, 0)
    ]
    assert len(expecting_rows) >= 3, (
        "at least one join path and one RPC must expect rows, or the canary cannot go red on a "
        "server that returns too few"
    )


def test_the_canary_is_a_strict_subset_so_the_full_suite_stays_available() -> None:
    """Tier 2 is reported, not gated, but it is still the record of what the model did."""
    canary_ids = {case["id"] for case in _cases(CANARY)}
    generated_ids = {case["id"] for case in _cases(GENERATED)}
    assert canary_ids < generated_ids, "the canary must be a strict subset of the full suite"


def test_every_canary_case_asserts_a_server_answer() -> None:
    """`score_case` defaults every check to True, so a case that checks nothing still reports green.

    The score dict now carries `asserted`: which checks this capability actually ran. Without it,
    `row_count_check: true` on a discovery case reads as an assertion that was made and passed when
    nothing was checked. 7 of the 33 generated cases assert no server answer at all; the canary
    asserts none of them.
    """
    from run_eval import score_case

    server_checks = {"answer_check", "evidence_check", "row_count_check"}
    for case in _cases(CANARY):
        asserted = set(score_case(case, [], "", error="", strict=True)["asserted"])
        assert server_checks & asserted, (
            f"{case['id']} asserts no server answer, so it measures the model's turn discipline"
        )


def test_the_canary_distinguishes_a_server_that_answers_from_one_that_does_not() -> None:
    """The property the whole tier-1 set exists for, stated once and directly.

    A canary is only a canary if its verdict changes when the server does. Feed every case the same
    server state - the signed path taken, nothing returned - and the set has to split. If it
    reported green both here and in `test_every_canary_case_detects_a_server_bug_in_its_own_direction`,
    the two together would be a contradiction, and the one that broke first would be the false one.
    """
    from run_eval import score_case

    accepts_silence = []
    rejects_silence = []
    for case in _cases(CANARY):
        event = {
            "name": "benthic_join" if case["id"].startswith("join_") else "benthic_rpc",
            "ok": True,
            "arguments": _route_arguments(case),
            "structured": {"row_count": 0, "rows": [], "truncated": False, "joins": [{"reliability": "partial"}]},
        }
        score = score_case(case, [event], "Done.", strict=True)
        passed = score["answer_check"] and score["row_count_check"]
        if passed:
            accepts_silence.append(case["id"])
        else:
            rejects_silence.append(case["id"])
    # The guards against a join that is too WIDE are satisfied by silence; the guards against a join
    # that is too NARROW are not. Both halves have to be present or the verdict cannot move.
    assert rejects_silence, "no case rejects a silent server, so a server that answers nothing goes green"
    assert accepts_silence, "every case rejects silence, which means the zero guards are gone too"
