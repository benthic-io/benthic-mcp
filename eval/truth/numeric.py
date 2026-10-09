"""Numeric truth: cases whose expected value is a number computed from the database.

The 30-case suite asks the model to name relations, columns and signed paths, and to report how many
rows a join returned. **Not one asks it to compute a number and checks the number.** That gap is the
whole reason a `order=` bug that reported the largest `total_obligation` as 2,698,943 when the
database says 373,109,113,199 could pass every case in the suite: nothing in it was asking for a
number.

Three properties this has to have, and each is a way a numeric suite goes wrong:

- **The expected value comes from SQL, not from the MCP server.** A number fetched through `benthic_query`
  would inherit every bug the suite exists to catch. Direct PostgREST with an explicit `order=` and
  `limit=1`, or a `count`, and the value is compared against that.
- **Tolerances are declared, not implied.** Money rounded to the cent, counts written with thousands
  separators, and a model answering "373 billion" rather than the full figure are all the same answer.
  Each case states how it may be rendered, and the comparison applies that.
- **A wrong number fails even when the route was right.** `query_order_mixed` passed for weeks while
  returning five recipients at $0.00, because only the route was checked. These cases check the number.

Written by hand these expectations would be worthless - a number typed by whoever wrote the case
describes whatever they believed, which is the rubber-stamp problem `classify.py` refuses. So they are
derived, and the derivation is re-runnable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# One pattern, one number. An earlier version ran four overlapping patterns and read "$1,234.56" as
# four numbers - 1.0, 234.56 and 1234.56 twice - so a wrong answer containing any of those fragments
# would pass. Order matters: the most specific form is tried first and scanning continues after it, so
# "$373,109,113,199.00" yields one number rather than a dozen fragments.
_NUMERIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    # currency with grouping: $373,109,113,199.00
    re.compile(r"\$\s*-?\d{1,3}(?:,\d{3})+(?:\.\d+)?"),
    # word form with a magnitude: 373 billion
    re.compile(r"-?\d+(?:\.\d+)?\s*(?:billion|million|thousand|bn|[bmk])\b", re.IGNORECASE),
    # grouped integer or decimal: 1,234.56
    re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?"),
    # bare number: 42 or 42.5
    re.compile(r"-?\d+(?:\.\d+)?"),
)

_MULTIPLIERS = {
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "b": 1_000_000_000,
    "million": 1_000_000,
    "m": 1_000_000,
    "thousand": 1_000,
    "k": 1_000,
}


@dataclass(frozen=True)
class NumericCase:
    """One question whose answer is a number, with the number derived from the database."""

    id: str
    question: str
    relation: str
    kind: str
    value: float | int
    column: str | None = None
    where: dict[str, Any] | None = None
    relative_tolerance: float = 0.0
    absolute_tolerance: float = 0.0
    allow_word_form: bool = True

    def as_case(self) -> dict[str, Any]:
        """Shape the graded suite's cases take, so the same runner and grader read it."""
        expected: dict[str, Any] = {
            "relation": self.relation,
            "kind": self.kind,
            "value": self.value,
            "relative_tolerance": self.relative_tolerance,
            "absolute_tolerance": self.absolute_tolerance,
            "allow_word_form": self.allow_word_form,
        }
        if self.column:
            expected["column"] = self.column
        if self.where:
            expected["where"] = self.where
        return {
            "id": self.id,
            "capability": "numeric_aggregate",
            "question": self.question,
            "expected": expected,
            "forbidden_claims": [],
            "required_tools": ["benthic_query"],
        }


def numbers_in(text: str) -> list[float]:
    """Every number the answer states, however it wrote it, with no duplicates and no fragments.

    One left-to-right pass, most specific pattern first, so "$1,234.56" is one number rather than four.
    Permissive about form and strict about value: "$373,109,113,199.00", "373 billion" and
    "373109113199" are the same answer and all have to be readable, because a grader that demands one
    spelling fails correct answers and gets switched off.
    """
    found: list[float] = []
    position = 0
    length = len(text)
    while position < length:
        best: tuple[int, re.Match[str]] | None = None
        for pattern in _NUMERIC_PATTERNS:
            match = pattern.search(text, position)
            if match is not None and (best is None or match.start() < best[0]):
                best = (match.start(), match)
        if best is None:
            break
        start, match = best
        raw = match.group(0)
        digits = re.sub(r"[^\d.\-]", "", raw.replace("$", ""))
        try:
            value = float(digits)
        except ValueError:
            value = 0.0
        unit = re.search(r"(billion|million|thousand|bn|[bmk])\b", raw, re.IGNORECASE)
        if unit is not None:
            value *= _MULTIPLIERS[unit.group(1).lower()]
        found.append(value)
        position = match.end()
    return found


def matches(value: float | int, candidate: float, relative: float, absolute: float) -> bool:
    """Whether `candidate` is the same number as `value` within the declared tolerance.

    Relative for anything large enough that the answer may be rounded, absolute for anything small
    enough that it may not. Both apply when both are declared, because a case stating one is stating
    the other as zero.
    """
    difference = abs(float(candidate) - float(value))
    if difference <= absolute:
        return True
    if relative > 0 and difference <= relative * abs(float(value)):
        return True
    return False


def check_numeric(case: dict[str, Any], case_run: dict[str, Any], answer: str) -> list[dict[str, Any]]:
    """Grade one numeric case. Returns checks in the same shape the grader emits."""
    expected = case.get("expected") or {}
    value = expected.get("value")
    checks: list[dict[str, Any]] = []
    if value is None:
        return [
            {
                "check": "numeric_expected_present",
                "ok": False,
                "expected": "a derived value",
                "found": None,
                "detail": "a numeric case with no expected value cannot be graded",
            }
        ]

    relative = float(expected.get("relative_tolerance") or 0.0)
    absolute = float(expected.get("absolute_tolerance") or 0.0)
    candidates = numbers_in(answer)

    checks.append(
        {
            "check": "states_a_number",
            "ok": bool(candidates),
            "expected": "at least one number in the answer",
            "found": candidates[:5] or "none",
            "detail": "" if candidates else "the question asks for a figure and the answer states none",
        }
    )
    hit = [candidate for candidate in candidates if matches(value, candidate, relative, absolute)]
    checks.append(
        {
            "check": "number_is_correct",
            "ok": bool(hit),
            "expected": value,
            "found": candidates[:5] or "none",
            "detail": ""
            if hit
            else (
                f"the answer states {candidates[:5]} and none is {value} within "
                f"rel={relative} abs={absolute}. A correct route with a wrong number fails here: "
                "query_order_mixed passed for weeks while returning five recipients at $0.00"
            ),
        }
    )
    if expected.get("column"):
        column = str(expected["column"])
        # Two spellings, two boundaries. `_` has to count as a word character, or `duns` matches
        # inside `duns_number` and the case credits a column that was never named. But the same column
        # is also written in prose with spaces - "total obligation" - and for that spelling an
        # underscore is a boundary. So the identifier form and the prose form are matched separately
        # rather than by transforming one into the other.
        named = bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(column)}(?![A-Za-z0-9_])", answer)) or bool(
            re.search(rf"(?<![A-Za-z0-9_]){re.escape(column.replace('_', ' '))}(?![A-Za-z0-9_])", answer)
        )
        checks.append(
            {
                "check": "names_the_column",
                "ok": named,
                "expected": column,
                "found": "named" if column in answer else "not named",
                "detail": "so a reader can tell which figure was asked for",
            }
        )
    return checks


def load_cases(path: str) -> list[dict[str, Any]]:
    document = json.loads(open(path, encoding="utf-8").read())
    return list(document.get("cases", []))
