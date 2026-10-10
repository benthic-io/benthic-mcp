"""Contracts for paraphrase invariance, and for the suite not being vacuous.

The nine grader defects found on 2026-10-09 shared one shape: a check read the wording it expected
rather than the wording models produce. Each was found by reading one failure that happened to surface.
Nothing asked the question this file asks - **does a verdict survive a correct answer being worded
differently?**

The corpus is the stored transcripts that carry text, across both 30-case reps. Each is a real model
answer graded against a case whose expectations were derived from the database, so a baseline verdict is
not an opinion.

Two parts, and the second is why the first is worth anything:

1. every transform leaves every baseline verdict unchanged
2. each of tonight's defects, put back into the grader, fails a correct answer that currently passes

Without part 2 a green part 1 means nothing - the transforms could simply be too weak to see anything.
That is the failure mode of every suite that grades itself, so it is tested rather than assumed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval" / "truth"))
sys.path.insert(0, str(ROOT / "src"))


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


grade = _load("grade", ROOT / "eval" / "truth" / "grade.py")
paraphrase = _load("paraphrase", ROOT / "eval" / "truth" / "paraphrase.py")
run_suite = _load("run_suite", ROOT / "eval" / "truth" / "run_suite.py")

# Every stored run, not a hand-picked subset. Two runs did not contain the after-mention rejection
# that the dot-boundary defect needs, so the suite went green with a blind spot in it and looked like
# the defect had been covered. A corpus chosen by its author is a corpus with holes in it.
RUNS = sorted(path.name for path in (ROOT / "eval" / "truth" / "runs").glob("run-*.json"))

_RELATIONS: set[str] = set()
_COLUMNS: set[str] = set()


def corpus() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(case, recorded run) for every transcript that carries text and is in the graded suite."""
    global _RELATIONS, _COLUMNS
    if not _RELATIONS:
        _RELATIONS, _COLUMNS = asyncio.run(run_suite.manifest_names())
    cases = {str(c["id"]): c for c in grade.load_cases(str(ROOT / "eval" / "generated" / "questions.json"))}
    out = []
    for name in RUNS:
        path = ROOT / "eval" / "truth" / "runs" / name
        if not path.is_file():
            continue
        out.extend(
            (cases[str(recorded["id"])], recorded)
            for recorded in json.loads(path.read_text()).get("runs", [])
            if str(recorded.get("id")) in cases and str(recorded.get("answer") or "").strip()
        )
    return out


def judge(case: dict[str, Any], run: dict[str, Any]) -> Any:
    return grade.grade_case(case, run, _RELATIONS, _COLUMNS)


def failures() -> list[tuple[str, str]]:
    """Every (case id, failing check) across the corpus, judged as it stands."""
    return sorted(
        {(str(case["id"]), check.name) for case, run in corpus() for check in judge(case, run).checks if not check.ok}
    )


def verdict_changes() -> list[tuple[str, str, str]]:
    """Every (case id, transform, newly-failing check) in the corpus."""
    found: set[tuple[str, str, str]] = set()
    for case, run in corpus():
        base = judge(case, run)
        if not base.passed:
            continue
        for name, _ in paraphrase.TRANSFORMS:
            alt = judge(case, paraphrase.transform_answer(run, name, case))
            if alt.passed:
                continue
            for check in alt.checks:
                if not check.ok and _was_ok(base, check.name):
                    found.add((str(case["id"]), name, check.name))
    return sorted(found)


def _was_ok(result: Any, check_name: str) -> bool:
    return next((c.ok for c in result.checks if c.name == check_name), True)


# --------------------------------------------------------------------------------------------
# Part 1: the transforms do not change a verdict.


def test_no_transform_changes_a_verdict_over_the_whole_corpus() -> None:
    """The suite itself, measured at about three seconds.

    It was run three times before it went green. The first reported 62 verdict changes, and every one
    was a defect in my transforms rather than in the grader:

    - the identifier regex matched `all_entities` independently of `usaspending.`, so backticking
      produced `` usaspending.`all_entities` `` - text no model writes, which broke `_mentions` for
      every qualified relation
    - `prose_columns` rewrote relations and trap names, which are things a case requires to be named
    - `group_numbers` rewrote a numeric UEI, which is digit-shaped but an identifier

    All three were fixed by constraining the transforms, not by relaxing the grader. The distinction
    matters: tuning a transform until the suite passes is the same failure mode as loosening a check,
    and this comment records which of the three it was.
    """
    changes = verdict_changes()
    assert not changes, changes[:8]


def test_the_corpus_is_not_empty_and_is_not_one_sided() -> None:
    """A corpus of only passing answers tests one direction.

    It cannot see a transform turning a failing answer into a pass, which is the more dangerous
    direction. The stored runs carry both.
    """
    entries = corpus()
    passes = sum(1 for case, run in entries if judge(case, run).passed)
    assert len(entries) >= 40, f"corpus too small to mean anything: {len(entries)}"
    assert 0 < passes < len(entries), f"the corpus is one-sided: {passes}/{len(entries)} pass"


def test_each_transform_is_a_real_change_to_the_text() -> None:
    """A transform that is the identity function cannot catch anything.

    Guarded by construction, because a silently no-op transform is the easy way to make part 1 green.
    """
    sample = "read  usaspending.all_entities where total_obligation is 1234   and the zero-row result is empty"
    for name, apply in paraphrase.TRANSFORMS:
        assert apply(sample, {"total_obligation"}) != sample, f"{name} changed nothing"


def test_the_transforms_leave_a_number_worth_of_numbers_alone_in_the_right_places() -> None:
    """`group_numbers` must rewrite a number and must not rewrite an asserted identifier.

    Both halves are asserted, so a transform that simply stopped rewriting numbers would pass a
    weaker test and quietly stop testing anything.
    """
    rewritten = paraphrase.group_numbers("the count is 10545", set())
    assert "10,545" in rewritten
    guarded = paraphrase.group_numbers("the left key is 142362594", {"142362594"})
    assert "142,362,594" not in guarded
    assert "142362594" in guarded


def test_prose_columns_leaves_a_relation_and_a_trap_alone() -> None:
    """A relation is `dataset.relation` and so is a trap name; neither is a column."""
    text = "legislator_terms not mv_current_lawmakers, on column total_obligation"
    rewritten = paraphrase.prose_columns(
        text, {"total_obligation", "usp_cl.legislator_terms", "usp_cl.mv_current_lawmakers"}
    )
    assert "total obligation" in rewritten
    assert "legislator terms" not in rewritten, rewritten
    assert "mv current lawmakers" not in rewritten, rewritten


# --------------------------------------------------------------------------------------------
# Part 2: the suite detects the defects it exists to detect.

#: Each entry puts one 2026-10-09 defect back and names the check that must then fail.
#: If an entry stops failing a case that currently passes, part 1 has stopped being able to see its
#: own subject and is decoration.
DEFECTS: tuple[tuple[str, str, str], ...] = (
    (
        "a count written as a word is graded as unstated",
        "reports_row_count",
        "digit_count",
    ),
    (
        "a sentence boundary inside dataset.relation closes the trap window",
        "did_not_answer_from_trap",
        "dot_boundary",
    ),
    (
        "the floor is given no columns, so an alias reads as an invented relation",
        "unmanifested_relation",
        "no_columns",
    ),
)


def _reintroduce_digit_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `zero rows` defect: `states_count` searched for `str(expected)`."""

    def digit_only(text: str, expected: Any) -> bool:
        return grade._mentions(text, str(expected))

    monkeypatch.setattr(grade, "states_count", digit_only)


def _reintroduce_dot_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `find(".")` defect: the period inside `usp_cl.mv_current_lawmakers` ended the window."""

    def any_period(text: str, position: int, default: int) -> int:
        best = default
        for match in re.finditer(r"[.!?]", text[:position]):
            best = max(best, match.end() - 1)
        return best

    def first_period(text: str, position: int) -> int:
        match = re.search(r"[.!?]", text[position:])
        return position + match.start() if match else len(text)

    monkeypatch.setattr(grade, "_last_sentence_break", any_period)
    monkeypatch.setattr(grade, "_next_sentence_break", first_period)


def _reintroduce_no_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner's defect: `column_names` never reached `grade_case`, so no alias check ran."""

    async def no_columns() -> tuple[set[str], set[str]]:
        return _RELATIONS, set()

    monkeypatch.setattr(run_suite, "manifest_names", no_columns)
    global _COLUMNS
    _COLUMNS = set()


_REINTRODUCE = {
    "digit_count": _reintroduce_digit_count,
    "dot_boundary": _reintroduce_dot_boundary,
    "no_columns": _reintroduce_no_columns,
}


@pytest.mark.parametrize(("description", "check_name", "kind"), DEFECTS)
def test_a_known_defect_fails_a_answer_that_currently_passes(
    description: str, check_name: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity.

    Part 1 going green proves nothing on its own - the transforms could be too weak to see anything.
    This puts each defect back and requires it to break an answer that currently passes. A defect the
    suite cannot see is a defect the suite would let through again, and that is the whole question being
    asked of it.

    The defects are reintroduced by swapping the fixed behaviour for the broken one, so the assertion
    is about the fix rather than about a hand-written example of the bug.
    """
    before = failures()
    _REINTRODUCE[kind](monkeypatch)
    after = failures()
    monkeypatch.undo()
    global _COLUMNS
    _RELATIONS, _COLUMNS = asyncio.run(run_suite.manifest_names())

    newly = [item for item in after if item not in before and item[1] == check_name]
    assert newly, f"the suite did not detect: {description}"
