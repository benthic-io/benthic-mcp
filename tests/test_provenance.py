"""A lesson has to say where it came from, or it cannot be attributed to anything.

`question_ref` is a hash of the question that produced the lesson, which is what lets a later run
ask whether that lesson fixed that case. A voluntary `benthic_report` need not carry a question, and
hashing the empty string produced a plausible-looking reference that resolved to nothing, so a
lesson with no traceable source was indistinguishable from one that had a source.
"""

import hashlib
from typing import Any

import pytest

from benthic_mcp.service import BenthicService

pytestmark = pytest.mark.asyncio


def runtime(catalog) -> Any:
    class Runtime:
        def __init__(self) -> None:
            self.catalog = catalog
            self.playbook = None
            self.result = None

    return Runtime()


async def service_for(catalog, settings) -> BenthicService:
    service = BenthicService(settings)
    service.playbook = lambda: _completed(runtime(catalog))  # type: ignore[method-assign]
    return service


async def _completed(value: Any) -> Any:
    return value


async def test_a_report_with_no_question_records_no_reference(catalog, settings) -> None:
    service = await service_for(catalog, settings)
    try:
        report = await service.record_lesson(symptom="guessed a column", lesson="check the schema")
        record = service.lesson_store.get(report.lesson_id)

        assert record is not None
        assert record.question_ref == ""
    finally:
        await service.close()


async def test_a_report_with_a_question_records_a_resolvable_reference(catalog, settings) -> None:
    service = await service_for(catalog, settings)
    try:
        report = await service.record_lesson(
            symptom="guessed a column",
            lesson="check the schema",
            question_summary="which district did the CA representative run in during 2019",
        )
        record = service.lesson_store.get(report.lesson_id)

        assert record is not None
        assert (
            record.question_ref
            == hashlib.sha256(b"which district did the CA representative run in during 2019").hexdigest()[:16]
        )
    finally:
        await service.close()


async def test_an_absent_reference_is_distinguishable_from_a_present_one(catalog, settings) -> None:
    service = await service_for(catalog, settings)
    try:
        without = await service.record_lesson(symptom="a", lesson="b")
        with_question = await service.record_lesson(symptom="c", lesson="d", question_summary="a question")

        absent = service.lesson_store.get(without.lesson_id)
        present = service.lesson_store.get(with_question.lesson_id)

        assert absent is not None and present is not None
        assert absent.question_ref != present.question_ref
    finally:
        await service.close()


def test_a_join_that_returns_the_wrong_row_count_is_a_failure_not_a_pass() -> None:
    """The scorer checked the route and never the result, for 695 case-runs.

    A signed join whose two endpoint columns are declared as different scalar types compared
    '03' against 3 in Python, matched nothing, and returned 0 rows where the catalog says there
    are 127. Both affected cases scored as passes because `join_check` only compared the argument
    slots of the call. This is the contract that would have caught it.
    """
    from run_eval import score_case

    case = {
        "id": "contract_join_answer",
        "capability": "identifier_partial_join",
        "expected": {
            "join_path": {
                "left": "usaspending.all_entities",
                "left_column": "congressional_district",
                "right": "usp_cl.legislator_terms",
                "right_column": "district",
                "reliability": "partial",
            },
            "right_count": 127,
            "right_keys_distinct": [3],
        },
    }
    join = {
        "name": "benthic_join",
        "ok": True,
        "arguments": {
            "left_source": "usaspending.all_entities",
            "right_source": "usp_cl.legislator_terms",
            "left_column": "congressional_district",
            "right_column": "district",
        },
        "structured": {
            "row_count": 0,
            "rows": [],
            "truncated": False,
            "joins": [{"reliability": "partial"}],
        },
    }

    score = score_case(case, [join], "Both sides signed and reliable.", strict=True)

    assert score["join_check"] is True, "the route is correct, which is what the old scorer saw"
    assert score["answer_check"] is False
    assert score["passed"] is False


def test_a_truncated_result_at_the_default_limit_still_counts_as_answered() -> None:
    """A bounded query that fills its limit has answered as far as it was asked to.

    district 3 in Maryland is 127 rows. The default limit returns 100 with truncated: true, which is
    a correct bounded answer, and reading it as a wrong one is how a fixed server stayed red.
    """
    from run_eval import score_case

    case = {
        "id": "contract_join_truncated",
        "capability": "identifier_partial_join",
        "expected": {
            "join_path": {
                "left": "usaspending.all_entities",
                "left_column": "congressional_district",
                "right": "usp_cl.legislator_terms",
                "right_column": "district",
                "reliability": "partial",
            },
            "right_count": 127,
            "right_keys_distinct": [3],
        },
    }
    join = {
        "name": "benthic_join",
        "ok": True,
        "arguments": {
            "left_source": "usaspending.all_entities",
            "right_source": "usp_cl.legislator_terms",
            "left_column": "congressional_district",
            "right_column": "district",
        },
        "structured": {
            "row_count": 100,
            "truncated": True,
            "rows": [{"right.district": 3}],
            "joins": [{"reliability": "partial"}],
        },
    }

    assert score_case(case, [join], "Signed, partial.", strict=True)["answer_check"] is True


def test_right_keys_are_compared_as_distinct_values() -> None:
    """127 rows share one key. A per-row expectation describes a data shape that does not exist."""
    from run_eval import score_case

    case = {
        "id": "contract_join_keys",
        "capability": "identifier_partial_join",
        "expected": {
            "join_path": {
                "left": "l",
                "left_column": "k",
                "right": "r",
                "right_column": "district",
                "reliability": "partial",
            },
            "right_count": 3,
            "right_keys_distinct": [3],
        },
    }
    rows = [{"right.district": 3} for _ in range(3)]

    def score(count: int) -> bool:
        return score_case(
            case,
            [
                {
                    "name": "benthic_join",
                    "ok": True,
                    "arguments": {
                        "left_source": "l",
                        "right_source": "r",
                        "left_column": "k",
                        "right_column": "district",
                    },
                    "structured": {
                        "row_count": count,
                        "truncated": False,
                        "rows": rows,
                        "joins": [{"reliability": "partial"}],
                    },
                }
            ],
            "Signed.",
            strict=True,
        )["answer_check"]

    assert score(3) is True
    assert score(0) is False


def test_a_left_join_returning_one_row_with_a_null_key_counts_as_zero_matches() -> None:
    """The shape that keeps a must-pass golden case red when read as row_count.

    benthic_join in left mode keeps every left row and fills the right with null, so "the right side
    is empty" comes back as one row. Counting returned rows made a correct answer read as wrong, and
    the case that asserts on it had been failing 5 of 27 runs for a long time for reasons that had
    nothing to do with the server.
    """
    from run_eval import score_case

    case = {
        "id": "contract_left_join",
        "capability": "identifier_join",
        "expected": {
            "join_path": {
                "left": "l",
                "left_column": "uei",
                "right": "r",
                "right_column": "uei",
                "reliability": "reliable",
            },
            "right_count": 0,
        },
    }

    def score(mode: str, row_count: int) -> bool:
        return score_case(
            case,
            [
                {
                    "name": "benthic_join",
                    "ok": True,
                    "arguments": {
                        "left_source": "l",
                        "right_source": "r",
                        "left_column": "uei",
                        "right_column": "uei",
                        "mode": mode,
                    },
                    "structured": {
                        "row_count": row_count,
                        "truncated": False,
                        "rows": [{"left.uei": "A", "right.uei": None}],
                        "joins": [{"reliability": "reliable"}],
                    },
                }
            ],
            "Signed, reliable, right side empty.",
            strict=True,
        )["answer_check"]

    assert score("left", 1) is True, "a left join that matched nothing is still the right answer"
    assert score("inner", 1) is False, "an inner join returning one row genuinely joined something"


def test_a_zero_row_expectation_asserts_every_call_not_the_luckiest_one() -> None:
    """`any()` over all join calls meant the emptiest one could carry a case.

    A case with `right_count: 0` exists to catch a join that is too wide - one that matches rows it
    should not. If any single call returning 0 satisfies it, a session that first returns 3 rows down
    the signed path and then 0 passes, which is the exact regression the case was written for. 48
    stored passing case-runs had a call on the signed path contradicting the expectation.
    """
    from run_eval import score_case

    case = {
        "id": "contract_no_false_join",
        "capability": "heuristic_heuristic_join",
        "expected": {
            "join_path": {
                "left": "l",
                "left_column": "duns",
                "right": "r",
                "right_column": "ein",
                "reliability": "heuristic",
            },
            "right_count": 0,
        },
    }

    def call(row_count: int) -> dict:
        return {
            "name": "benthic_join",
            "ok": True,
            "arguments": {
                "left_source": "l",
                "right_source": "r",
                "left_column": "duns",
                "right_column": "ein",
            },
            "structured": {
                "row_count": row_count,
                "truncated": False,
                "rows": [{"r.ein": "x"}] * row_count,
                "joins": [{"reliability": "heuristic"}],
            },
        }

    def score(calls: list[dict]) -> bool:
        return score_case(case, calls, "No match.", strict=True)["answer_check"]

    assert score([call(0)]) is True
    assert score([call(3), call(0)]) is False, "a false match anywhere on the signed path is the regression"
    assert score([call(3)]) is False


def test_an_rpc_case_is_satisfied_only_by_the_operation_and_arguments_it_asked_for() -> None:
    """The RPC check compared row_count alone, so any call with the right number of rows passed.

    `expected.arguments` and `expected.operation` were recorded by the generator and never read. A
    districts_in_bbox case expects 1 row for one specific bounding box; 33 stored case-runs satisfied
    it on row_count with a box the model invented instead, including a zero-height one. Four of the
    six RPC cases expect row_count 0, so a server returning nothing at all passed all four.
    """
    from run_eval import score_case

    case = {
        "id": "contract_rpc_arguments",
        "capability": "districts_in_bbox_rpc",
        "expected": {
            "operation": "districts_in_bbox",
            "arguments": {
                "min_lon": 1.2823954,
                "min_lat": 52.6185576,
                "max_lon": 1.3023954,
                "max_lat": 52.6385576,
            },
            "row_count": 1,
        },
    }

    def score(operation: str | None, arguments: dict | None, row_count: int) -> bool:
        event = {
            "name": "benthic_rpc",
            "ok": True,
            "arguments": {k: v for k, v in {"operation": operation, **(arguments or {})}.items() if v},
            "structured": {"row_count": row_count},
        }
        return score_case(case, [event], "One district.", strict=True)["row_count_check"]

    expected_args = case["expected"]["arguments"]
    assert score("districts_in_bbox", expected_args, 1) is True
    assert score("find_district", expected_args, 1) is False, "a different RPC is not an answer"
    assert score("districts_in_bbox", {"min_lon": 38.9, "min_lat": -77.1, "max_lon": 40.5, "max_lat": -74.2}, 1) is (
        False
    ), "a box the model invented is not the box the case asked about"
    assert score("districts_in_bbox", expected_args, 6) is False
