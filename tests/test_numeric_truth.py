"""Contracts for numeric truth.

A numeric grader has one dangerous failure mode: it reads a number wrongly and reports a correct answer
as wrong, or reads a wrong number the way the case expected and reports a bug that is not there. Both
look like findings, so both directions are tested for every shape, and the number parser is round-
tripped against Python's own formatting rather than trusted.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("numeric", ROOT / "eval" / "truth" / "numeric.py")
assert _spec is not None and _spec.loader is not None
numeric = importlib.util.module_from_spec(_spec)
sys.modules["numeric"] = numeric
_spec.loader.exec_module(numeric)


def case(**expected: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"relation": "a.b", "kind": "count", "value": 42}
    base.update(expected)
    return {"id": "n", "capability": "numeric_aggregate", "question": "q", "expected": base, "forbidden_claims": []}


def graded(answer: str, **expected: Any) -> list[dict[str, Any]]:
    return numeric.check_numeric(case(**expected), {"answer": answer}, answer)


def failing(answer: str, **expected: Any) -> list[str]:
    return [c["check"] for c in graded(answer, **expected) if not c["ok"]]


# --------------------------------------------------------------------------------------------
# The parser, round-tripped against Python's own formatting.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42", 42.0),
        ("The answer is 42 rows.", 42.0),
        ("$1,234.56", 1234.56),
        ("373,109,113,199.00", 373109113199.0),
        ("-17", -17.0),
        ("0", 0.0),
    ],
)
def test_ordinary_spellings_parse_to_the_right_number(text: str, expected: float) -> None:
    assert expected in numeric.numbers_in(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("about 373 billion", 373_000_000_000.0),
        ("roughly 2.7 million", 2_700_000.0),
        ("1.5 billion dollars", 1_500_000_000.0),
        ("some 250 thousand", 250_000.0),
    ],
)
def test_word_forms_parse(text: str, expected: float) -> None:
    """A model answering "373 billion" rather than the full figure is the same answer."""
    assert expected in numeric.numbers_in(text)


def test_a_number_is_read_the_same_way_python_writes_it() -> None:
    """Round-trip, so the parser is checked against a formatter nobody here hand-tuned."""
    for value in (0, 1, 42, 1234, 999999, 1234567890, 373109113199, 2.5, 0.001):
        rendered = f"{value:,}"
        assert float(value) in numeric.numbers_in(rendered), f"{value:,} did not round-trip"
        assert float(value) in numeric.numbers_in(str(value))


def test_currency_and_grouping_do_not_produce_two_numbers() -> None:
    """ "$1,234.56" is one number. Reading it as 1 and 1234.56 would let a wrong answer pass."""
    assert numeric.numbers_in("$1,234.56") == [1234.56]


def test_a_bare_year_is_a_number_the_grader_can_see() -> None:
    """It will be read, and that is a known cost: a case whose answer quotes a year needs care.

    Stated as a fact about the instrument rather than papered over - `states_a_number` can be satisfied
    by a year in an otherwise answerless response, which is why the cases below require the value too.
    """
    assert 117.0 in numeric.numbers_in("the 117th Congress")


# --------------------------------------------------------------------------------------------
# Tolerance: exact by default, tolerant where rounding is legitimate.


def test_an_exact_match_passes_with_no_tolerance_declared() -> None:
    assert "number_is_correct" not in failing("The count is 42.")


def test_a_wrong_number_fails_with_no_tolerance_declared() -> None:
    assert "number_is_correct" in failing("The count is 41.")
    detail = next(c["detail"] for c in graded("The count is 41.") if c["check"] == "number_is_correct")
    assert "query_order_mixed" in detail, "the failure must name the failure mode it guards"


def test_a_relative_tolerance_admits_rounding_but_not_a_different_figure() -> None:
    ok = failing("About 3.73e11, say 373 billion.", value=373109113199, relative_tolerance=0.01)
    assert "number_is_correct" not in ok
    bad = failing("It is 2,698,943.", value=373109113199, relative_tolerance=0.01)
    assert "number_is_correct" in bad, "the order= bug's figure must not be admitted"


def test_an_absolute_tolerance_admits_a_small_difference_only() -> None:
    assert "number_is_correct" not in failing("42.0", value=42, absolute_tolerance=0.05)
    assert "number_is_correct" in failing("42.5", value=42, absolute_tolerance=0.05)


def test_a_zero_tolerance_declared_is_enforced() -> None:
    assert "number_is_correct" in failing("42.0000001", value=42)


def test_the_order_bug_figure_is_rejected_against_the_true_maximum() -> None:
    """The specific bug this whole capability exists to catch.

    `order=` used to sort one page in Python, so the server reported the largest `total_obligation` as
    2,698,943 when the database says 373,109,113,199. Every other case in the suite passed throughout.
    """
    wrong = "The largest single total obligation is $2,698,943.00"
    right = "The largest single total obligation is $373,109,113,199.00"
    assert "number_is_correct" in failing(wrong, value=373109113199, column="total_obligation")
    assert "number_is_correct" not in failing(right, value=373109113199, column="total_obligation")


# --------------------------------------------------------------------------------------------
# Absence of a number is its own failure, distinct from a wrong one.


def test_an_answer_with_no_number_fails_states_a_number() -> None:
    assert "states_a_number" in failing("I could not determine that.")


def test_a_number_among_several_is_found_among_several() -> None:
    """ "There were 52 entities in district 03 and none in district ZZ" - the 52 is the answer."""
    assert "number_is_correct" not in failing(
        "There were 52 entities in district 03 and none in district ZZ.", value=52
    )


# --------------------------------------------------------------------------------------------
# The column must be named, so a reader can tell which figure was asked for.


def test_the_column_must_be_named_when_the_case_declares_one() -> None:
    assert "names_the_column" not in failing("total_obligation sums to 42.", value=42, column="total_obligation")
    assert "names_the_column" in failing("The value is 42.", value=42, column="total_obligation")


@pytest.mark.parametrize(
    ("column", "answer"),
    [
        ("ein", "The reinstate_id is 42."),  # inside a longer identifier
        ("name", "The surname is 42."),  # inside an ordinary word
        ("duns", "The duns_number is 42."),  # prefix of a longer word
    ],
)
def test_the_column_check_does_not_match_inside_another_word(column: str, answer: str) -> None:
    """`ein` inside `reinstate_id` and `name` inside `surname` are not the column.

    Crediting the column for an answer that never named it would let a case pass on a figure the reader
    cannot attribute to a question.
    """
    assert "names_the_column" in failing(answer, value=42, column=column)


def test_the_column_check_does_match_a_standalone_word() -> None:
    """ "The total is 42" does name a column called `total`. The previous version of this test asserted
    the opposite, which would have meant loosening the check to pass a test rather than to be right."""
    assert "names_the_column" not in failing("The total is 42.", value=42, column="total")


# --------------------------------------------------------------------------------------------
# A case with no expected value must not grade as a pass.


def test_a_numeric_case_with_no_value_is_reported_rather_than_passed() -> None:
    checks = numeric.check_numeric(
        {"id": "n", "capability": "numeric_aggregate", "question": "q", "expected": {"relation": "a.b"}},
        {"answer": "42"},
        "42",
    )
    assert not checks[0]["ok"]
    assert checks[0]["check"] == "numeric_expected_present"


# --------------------------------------------------------------------------------------------
# The suite file itself.


def test_every_numeric_case_declares_its_tolerances() -> None:
    """A case with no tolerance declared is an exact match, which is only right for counts."""
    path = ROOT / "eval" / "truth" / "numeric_cases.json"
    if not path.is_file():
        pytest.skip("no numeric cases generated yet")
    for case_ in numeric.load_cases(str(path)):
        expected = case_.get("expected") or {}
        assert "value" in expected, f"{case_['id']} has no derived value"
        assert (
            "relative_tolerance" in expected or "absolute_tolerance" in expected or expected.get("kind") == "count"
        ), f"{case_['id']} is not a count and declares no tolerance, so it demands an exact match"


def test_every_numeric_case_derives_its_value_and_says_how() -> None:
    """A number in the file with no stated provenance is exactly the rubber stamp this avoids."""
    path = ROOT / "eval" / "truth" / "numeric_cases.json"
    if not path.is_file():
        pytest.skip("no numeric cases generated yet")
    for case_ in numeric.load_cases(str(path)):
        assert case_.get("capability") == "numeric_aggregate"
        assert case_.get("derived_from"), f"{case_['id']} does not record where its value came from"
        assert case_.get("derived_at"), f"{case_['id']} does not record when it was derived"
