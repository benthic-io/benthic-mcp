"""Grade a graded suite against what actually happened. Deterministic and offline.

`eval/combination/classify.py` deliberately does not score, and for the ungraded 100-question suite
that is right: an `expected` written by whoever ran the tool last would describe whatever the server
happened to do, which is how a suite becomes a rubber stamp.

`eval/generated/questions.json` is a different asset. Its `expected` values are fetched from the live
database at generation time (`generate_cases.py` calls `fetch_rows` and `sample_key`) and its join
paths come from the signed manifest, so they are independent of the server's last behaviour. Those are
gradeable, and nothing was grading them.

Two rules this module holds to:

- **No single score.** Grading is per capability and reported as a per-capability table. An aggregate
  over ten capabilities hides the fact that RPCs are exact while joins are partly heuristic, and a
  suite that reports one number gets optimised until that number is high.
- **A failed check says what was expected and what was found.** "wrong" is not a finding.

The unit of grading is one case: the question text, the transcript the model produced, and the
`expected` block from generation. Grading never re-runs the model, so it is free and repeatable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Phrases that mean the model asserted a join the manifest does not sign. Checked against the answer
# text rather than against tool calls, because the failure that matters is the model telling the user
# something the catalog does not support - whether or not it also called the tool.
_UNSIGNED_JOIN_CLAIMS = (
    "unsigned join",
    "invented join",
    "unqualified exact match",
    "joined the relations",
)

# The RPCs return rows whose geometry is an approximation - a district's representative point or
# bounding box, not its boundary. Describing them as exact is a false claim about geography.
_GEOGRAPHIC_EXACTNESS = ("exact geographic identity",)

# A relation the model named that is not in the signed catalog. Built by the caller from the catalog;
# kept here so the check has one name.
UNMANIFESTED = "unmanifested_relation"


@dataclass(frozen=True)
class Check:
    """One assertion about one case. `ok=False` carries the evidence, never just a verdict."""

    name: str
    ok: bool
    expected: Any = None
    found: Any = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "ok": self.ok,
            "expected": self.expected,
            "found": self.found,
            "detail": self.detail,
        }


@dataclass
class CaseResult:
    id: str
    capability: str
    passed: bool
    checks: list[Check] = field(default_factory=list)
    question: str = ""
    tools_called: list[str] = field(default_factory=list)

    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "capability": self.capability,
            "passed": self.passed,
            "question": self.question,
            "tools_called": self.tools_called,
            "checks": [check.as_dict() for check in self.checks],
        }


def answer_text(case_run: dict[str, Any]) -> str:
    """Everything the model said, joined: the final answer plus its reasoning.

    Reasoning is included deliberately. A model can reach the right number by asserting in prose
    something it never verified, and a grader that reads only the final answer cannot see that.
    """
    parts: list[str] = []
    final = case_run.get("answer")
    if isinstance(final, str):
        parts.append(final)
    for turn in case_run.get("turns") or []:
        for key in ("reasoning", "text"):
            value = turn.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def tool_calls(case_run: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for turn in case_run.get("turns") or []:
        for result in turn.get("tool_results") or []:
            calls.append({"name": result.get("name") or "", "args": result.get("args") or {}, "ok": result.get("ok")})
    return calls


def tools_called(case_run: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for call in tool_calls(case_run):
        name = call["name"].replace("benthic_", "")
        if name not in seen:
            seen.append(name)
    return seen


def _mentions(haystack: str, needle: str) -> bool:
    """Word-shaped match: the identifier must appear as a token, not inside another word.

    A bare substring test is not a small imprecision here. `ein` occurs inside `reine`, `name` inside
    `surname`, and `uei` inside `unique_entity_identifier` - all of which are ordinary words and
    column names in this domain. Crediting a wrong column is worse than not grading: it manufactures
    a pass, and a grader that manufactures passes is one nobody should trust.

    Separators count as boundaries, because identifiers arrive as `dataset.relation`, `col name` in
    prose, and `COL_NAME` in a quoted column. The punctuation *inside* an identifier is the exception,
    which is why the dotted form is matched by stripping dots from both sides rather than splitting on
    them.
    """
    if not needle or not haystack:
        return False
    candidate = needle.strip().strip("`\"'")
    if not candidate:
        return False
    if "." in candidate:
        # `usaspending.prime_awards` may appear with a space or no separator at all.
        squashed = candidate.replace(".", "").replace(" ", "").replace("_", "")
        hay = haystack.replace(".", "").replace(" ", "").replace("_", "").lower()
        return squashed.lower() in hay
    pattern = r"(?<![A-Za-z0-9])" + re.escape(candidate) + r"(?![A-Za-z0-9])"
    if re.search(pattern, haystack, re.IGNORECASE):
        return True
    return bool(re.search(pattern, haystack.replace("_", " "), re.IGNORECASE))


def check_forbidden_claims(case: dict[str, Any], case_run: dict[str, Any]) -> list[Check]:
    """Every `forbidden_claims` phrase must be absent from what the model said.

    These are the false-claim cases and they matter more than the right-answer ones: a model that
    invents a join is worse than one that reports it cannot find one.
    """
    text = answer_text(case_run)
    lowered = text.lower()
    checks: list[Check] = []
    for phrase in case.get("forbidden_claims") or []:
        present = phrase.lower() in lowered
        checks.append(
            Check(
                name=f"must_not_claim:{phrase}",
                ok=not present,
                expected="phrase absent from the answer",
                found=phrase if present else "absent",
                detail="the model asserted a claim the signed catalog does not support" if present else "",
            )
        )
    checks.append(
        Check(
            name="must_not_claim:unsigned_join_variants",
            ok=not any(phrase.lower() in lowered for phrase in _UNSIGNED_JOIN_CLAIMS),
            expected="no unsigned-join phrasing",
            found="present" if any(phrase.lower() in lowered for phrase in _UNSIGNED_JOIN_CLAIMS) else "absent",
        )
    )
    for phrase in case.get("forbidden_claims") or []:
        if phrase in _GEOGRAPHIC_EXACTNESS:
            exact_claims = [
                marker
                for marker in ("exactly on", "precisely at", "exact geographic", "precisely located")
                if marker in lowered
            ]
            checks.append(
                Check(
                    name="must_not_overstate_geometry",
                    ok=not exact_claims,
                    expected="no exactness claim about an approximate geometry",
                    found=exact_claims or "none",
                    detail="the RPC returns a representative point or bounding box, not a boundary",
                )
            )
    return checks


def check_manifested_relations(
    case_run: dict[str, Any], signed_relations: set[str], column_names: set[str] | None = None
) -> Check:
    """No relation the model named may be absent from the signed manifest.

    This is the anti-hallucination floor and it is the check that would catch a model inventing a
    table to make an answer work. It is deliberately narrow: only `dataset.relation` pairs the model
    wrote in prose are considered, because guessing at relation-shaped words is not this check's job.
    """
    text = answer_text(case_run)
    invented = sorted(
        spelling
        for spelling in _relation_spellings(text)
        if _looks_like_a_relation(spelling)
        and spelling.replace("`", "").lower() not in signed_relations
        and not _is_a_column_alias(spelling, column_names)
    )
    return Check(
        name=UNMANIFESTED,
        ok=not invented,
        expected="every relation named is in the signed manifest",
        found=invented or "none",
        detail=""
        if not invented
        else f"{len(invented)} relation(s) named that the manifest does not carry: {invented[:4]}",
    )


# Filter syntax and abbreviations all contain a dotted pair that is not a relation. The first version of
# this check matched any dotted token and reported `eq.senate`, `not.is` and `u.s` as invented
# relations - `u.s` is what falls out of matching inside `usp_cl.legislator_terms`. A floor that reports
# relations nobody named is worse than no floor, because it converts real failures into noise.
_NOT_A_RELATION = {"in", "is", "not", "eq", "neq", "gt", "gte", "lt", "lte", "like", "null", "u", "s"}


def _relation_spellings(text: str) -> set[str]:
    """Dotted spellings whose first half is not filter syntax."""
    pairs = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", text))
    return {pair for pair in pairs if pair.split(".", 1)[0].lower() not in _NOT_A_RELATION}


# `left.state` and `right.uei` are the column aliases a model uses when it reports a join's evidence
# table. They are not invented relations, and treating them as such failed six of thirty cases in the
# first run for no reason at all.
_ALIAS_PREFIXES = {"left", "right", "s", "l", "r", "both", "lhs", "rhs"}


def _is_a_column_alias(spelling: str, column_names: set[str] | None) -> bool:
    dataset, _, relation = spelling.partition(".")
    if dataset.lower() in _ALIAS_PREFIXES:
        return True
    if not column_names:
        return False
    return relation.lower() in column_names and dataset.lower() not in {p.split(".")[0] for p in column_names}


def _looks_like_a_relation(spelling: str) -> bool:
    """Whether a dotted spelling is a relation reference rather than prose or a fragment.

    Both halves must be plausible identifiers: a relation in this catalog is written out in full, and a
    one-or-two character half is a fragment of a longer word rather than a name.
    """
    dataset, _, relation = spelling.partition(".")
    if dataset.lower() in _NOT_A_RELATION or relation.lower() in _NOT_A_RELATION:
        return False
    # A relation is written in lowercase snake case and always names a relation on the right. A dot
    # inside a capitalised token is an acronym written in prose: `SAM.gov` is the dataset's human name,
    # and `U.S.` is not an identifier at all.
    if not dataset.islower() or not relation.islower():
        return False
    return len(dataset) >= 3 and len(relation) >= 3


def check_discovery(case: dict[str, Any], case_run: dict[str, Any]) -> list[Check]:
    """The answer must name the expected relation and every expected column."""
    expected = case.get("expected") or {}
    text = answer_text(case_run)
    checks: list[Check] = []
    relation = expected.get("relation")
    if relation:
        checks.append(
            Check(
                name="names_expected_relation",
                ok=_mentions(text, relation),
                expected=relation,
                found="named" if _mentions(text, relation) else "not named",
                detail=""
                if _mentions(text, relation)
                else "the relation the question asked about is absent from the answer",
            )
        )
    for column in expected.get("columns") or []:
        present = _mentions(text, column)
        checks.append(
            Check(
                name=f"names_column:{column}",
                ok=present,
                expected=column,
                found="named" if present else "not named",
            )
        )
    if _requires(case, "discover"):
        calls = [call for call in tool_calls(case_run) if "discover" in call["name"]]
        checks.append(
            Check(
                name="used_discover",
                ok=bool(calls),
                expected="benthic_discover called",
                found=len(calls),
                detail="" if calls else "discovery is the required tool for this capability",
            )
        )
    return checks


def check_signed_path(case: dict[str, Any], case_run: dict[str, Any]) -> list[Check]:
    """Every join edge the answer relies on must be the signed edge, named column for column.

    A model that reaches the right row by an unsigned path has still told the user something the
    catalog does not license, so the columns are compared and not just the endpoints.
    """
    expected = case.get("expected") or {}
    path = expected.get("join_path") or {}
    paths = expected.get("paths") or ([path] if path else [])
    text = answer_text(case_run)
    checks: list[Check] = []
    for edge in paths:
        left, right = edge.get("left"), edge.get("right")
        left_column, right_column = edge.get("left_column"), edge.get("right_column")
        label = f"{left}.{left_column}->{right}.{right_column}"
        edge_ok = all(_mentions(text, part) for part in (left, right, left_column, right_column))
        checks.append(
            Check(
                name=f"cites_signed_edge:{label}",
                ok=edge_ok,
                expected=label,
                found="cited" if edge_ok else "not cited in full",
                detail=""
                if edge_ok
                else "the answer does not name both endpoints and both key columns of the signed edge",
            )
        )
    for key_name in ("right_count", "left_key", "hub", "key", "column"):
        value = expected.get(key_name)
        if value is None:
            continue
        present = _mentions(text, str(value))
        checks.append(
            Check(
                name=f"reports:{key_name}",
                ok=present,
                expected=value,
                found="present" if present else "absent",
            )
        )
    if _requires(case, "join"):
        calls = [call for call in tool_calls(case_run) if "join" in call["name"]]
        checks.append(
            Check(
                name="used_benthic_join",
                ok=bool(calls),
                expected="benthic_join called",
                found=len(calls),
            )
        )
    return checks


def check_rejection(case: dict[str, Any], case_run: dict[str, Any]) -> list[Check]:
    """`must_reject` means the correct outcome is a refusal, and that is a *pass*.

    The server's own text is the evidence, never the model's prose: prose matching finds filler, and
    "let me try that" is not a refusal. A model that says "there is no signed path" and one that says
    "no signed path, so I checked the other direction" are not the same answer, so the tool calls
    matter too.
    """
    text = answer_text(case_run)
    calls = tool_calls(case_run)
    refused_by_server = [call for call in calls if not call["ok"]]
    says_no_path = any(
        marker in text.lower()
        for marker in (
            "no signed",
            "not a signed",
            "does not sign",
            "no join path",
            "not authorized",
            "no such join",
            "cannot be joined",
            "no signed path",
            "authorizes no join",
            "authorises no join",
            "no path between",
            "no join between",
            "no join is",
            "are not joined",
            "cannot be joined",
            "not signed",
            "no relationship",
        )
    )
    invented = [call["name"] for call in calls if "join" in call["name"] and call["ok"]]
    return [
        Check(
            name="must_reject",
            ok=says_no_path or bool(refused_by_server),
            expected="the answer reports that no signed path exists",
            found="reported" if says_no_path else ("server refused" if refused_by_server else "neither"),
            detail=""
            if (says_no_path or refused_by_server)
            else "the model neither reported the absence nor was refused by the server",
        ),
        Check(
            name="did_not_execute_an_unsigned_join",
            ok=not invented,
            expected="no successful join call",
            found=invented or "none",
        ),
    ]


def check_rpc(case: dict[str, Any], case_run: dict[str, Any]) -> list[Check]:
    """The RPC must have been called with the expected arguments and the row count must be stated.

    The RPCs are the one place where the server computes the answer itself, so an argument mismatch is
    a different failure from a count mismatch and they are checked separately.
    """
    expected = case.get("expected") or {}
    calls = [call for call in tool_calls(case_run) if "rpc" in call["name"]]
    checks: list[Check] = []
    if _requires(case, "rpc"):
        checks.append(
            Check(
                name="used_benthic_rpc",
                ok=bool(calls),
                expected="benthic_rpc called",
                found=len(calls),
            )
        )
    wanted = dict(expected.get("arguments") or {})
    if calls and wanted:
        # Exact comparison. An earlier version of this check excused the coordinates because the
        # question said "a small bounding box around" a point without stating the extent, which made
        # grading a coin flip. That was a defect in the *suite*, not in the check: f2f0346 rewrote the
        # question to state the extents it asserts on, because a model had passed a zero-height box and
        # satisfied the case for the wrong reason. The question now names the box, so the arguments are
        # knowable and are compared exactly.
        sent = calls[0]["args"]
        mismatched = {
            key: {"expected": value, "sent": sent.get(key)}
            for key, value in wanted.items()
            if str(sent.get(key)) != str(value)
        }
        checks.append(
            Check(
                name="rpc_arguments_match",
                ok=not mismatched,
                expected=wanted,
                found=sent,
                detail="" if not mismatched else f"argument mismatch: {sorted(mismatched)}",
            )
        )

    row_count = expected.get("row_count")
    if row_count is not None:
        checks.append(
            Check(
                name="reports_row_count",
                ok=_mentions(answer_text(case_run), str(row_count)),
                expected=row_count,
                found="stated" if _mentions(answer_text(case_run), str(row_count)) else "not stated",
                detail="",
            )
        )
    return checks


def check_relation_trap(case: dict[str, Any], case_run: dict[str, Any]) -> list[Check]:
    """The answer must use the historical relation and must not answer from the trap."""
    expected = case.get("expected") or {}
    text = answer_text(case_run)
    relation, trap = expected.get("relation"), expected.get("trap")
    checks: list[Check] = []
    if relation:
        checks.append(
            Check(
                name="used_expected_relation",
                ok=_mentions(text, relation),
                expected=relation,
                found="named" if _mentions(text, relation) else "not named",
            )
        )
    if trap:
        # Naming the trap is not relying on it. A run graded this case failed here where the model had
        # written "I used the historical record usp_cl.legislator_terms (NOT mv_current_lawmakers which
        # is current-only)" - it named the trap in order to reject it, and said so explicitly.
        #
        # The test is therefore whether the trap is invoked without a rejection nearby. That is weaker
        # than reading the answer, and it is stated rather than presented as exact: a model that used
        # the trap and then criticised it would still pass. Every mention must be rejected, not just
        # one, because a single rejection must not launder a later reliance.
        rejected = _trap_is_rejected(text, trap)
        used_trap = _mentions(text, trap) and not rejected
        checks.append(
            Check(
                name="did_not_answer_from_trap",
                ok=not used_trap,
                expected=f"no reliance on {trap}",
                found="named and rejected" if rejected else ("relied on" if used_trap else "not named"),
                detail=""
                if not used_trap
                else f"{trap} cannot answer a historical question and nothing nearby sets it aside",
            )
        )
    return checks


_REJECTION_MARKERS = (
    "not ",
    "rather than",
    "instead of",
    "cannot",
    "current-only",
    "current only",
    "only the sitting",
    "would be wrong",
    "is wrong",
    "does not answer",
    "no historical",
)


def _trap_is_rejected(text: str, trap: str) -> bool:
    """Whether every mention of the trap sits inside a rejection.

    A window rather than the whole answer, because "not X" far from the mention of X is not a
    rejection of it.
    """
    lowered = text.lower()
    needle = trap.lower()
    positions = [match.start() for match in re.finditer(re.escape(needle), lowered)]
    if not positions:
        return False
    # The window is bounded by the neighbouring mentions, not by a fixed length. A fixed window large
    # enough to catch a rejection can reach across to the next mention of the trap and read that one's
    # rejection as its own - which lets "I avoided the trap. Reading from the trap gives the answer"
    # pass. Splitting at the boundaries keeps each judgement local.
    # The window stops at sentence boundaries, because that is where a rejection of *this* mention
    # lives. Bounding by neighbouring mentions is not enough: in "I avoided the trap because it is
    # current-only. Reading from the trap gives the answer", the second mention sits in the same
    # sentence-distance of the first rejection's wording and reads as rejected too.
    #
    # A rejection before the mention and a rejection after it count, since "not X, because ..." and
    # "... which is current-only" both reject X.
    for position in positions:
        end_of_sentence_before = max(lowered.rfind(".", 0, position), lowered.rfind("\n", 0, position))
        start = end_of_sentence_before + 1 if end_of_sentence_before >= 0 else max(0, position - 200)
        boundary_after = min(
            (index for index in (lowered.find(".", position), lowered.find("\n", position)) if index >= 0),
            default=len(lowered),
        )
        end = min(boundary_after, position + len(needle) + 200)
        if not any(marker in lowered[start:end] for marker in _REJECTION_MARKERS):
            return False
    return True


def check_sequential(case: dict[str, Any], case_run: dict[str, Any]) -> list[Check]:
    """Two lookups on a shared column, and the answer must not assert a join between them."""
    expected = case.get("expected") or {}
    text = answer_text(case_run)
    checks: list[Check] = []
    for key_name in ("left", "right", "column", "key", "right_count"):
        value = expected.get(key_name)
        if value is None:
            continue
        present = _mentions(text, str(value))
        checks.append(
            Check(
                name=f"reports:{key_name}",
                ok=present,
                expected=value,
                found="present" if present else "absent",
            )
        )
    return checks


# ---------------------------------------------------------------------------------------------
# Numeric truth. The 30-case suite names relations, columns and signed paths, and reports how many
# rows a join returned; not one asks the model to compute a figure and check it. That gap is why the
# order= bug could report the largest total_obligation as 2,698,943 when the database says
# 373,109,113,199 and pass every case. Expectations are derived from SQL by generate_numeric.py,
# never through benthic_query - a number fetched by the server under test inherits every defect the
# case exists to catch.

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


def check_numeric(case: dict[str, Any], case_run: dict[str, Any]) -> list[Check]:
    """Grade one numeric case: did the model state the derived figure, and is it the right one."""
    expected = case.get("expected") or {}
    value = expected.get("value")
    checks: list[Check] = []
    if value is None:
        return [
            Check(
                name="numeric_expected_present",
                ok=False,
                expected="a derived value",
                found=None,
                detail="a numeric case with no expected value cannot be graded",
            )
        ]

    relative = float(expected.get("relative_tolerance") or 0.0)
    absolute = float(expected.get("absolute_tolerance") or 0.0)
    record = answer_text(case_run)
    final = case_run.get("answer") if isinstance(case_run.get("answer"), str) else ""
    candidates = numbers_in(record)
    reported = numbers_in(final)

    checks.append(
        Check(
            name="states_a_number",
            ok=bool(candidates),
            expected="at least one number in the answer",
            found=candidates[:5] or "none",
            detail="" if candidates else "the question asks for a figure and the answer states none",
        )
    )
    hit = [candidate for candidate in candidates if matches(value, candidate, relative, absolute)]
    checks.append(
        Check(
            name="number_is_correct",
            ok=bool(hit),
            expected=value,
            found=candidates[:5] or "none",
            detail=""
            if hit
            else (
                f"the answer states {candidates[:5]} and none is {value} within "
                f"rel={relative} abs={absolute}. A correct route with a wrong number fails here: "
                "query_order_mixed passed for weeks while returning five recipients at $0.00"
            ),
        )
    )

    # The permissive check reads the record; this one reads the final answer. A model can compute the
    # right figure, say so while reasoning, and then report a different one - and the figure the reader
    # is shown is the final answer. Grading only the record would pass that, and grading only the final
    # answer would fail a model that put the figure in a table and deferred to it.
    shown = [candidate for candidate in reported if matches(value, candidate, relative, absolute)]
    checks.append(
        Check(
            name="figure_in_final_answer",
            ok=bool(shown),
            expected=value,
            found=reported[:5] or "none",
            detail=""
            if shown
            else (
                f"the final answer states {reported[:5] or 'no figure'}. "
                + (
                    "the figure is in the reasoning but not in the answer, so the reader never got it"
                    if hit
                    else "the question asked for the figure and the answer does not carry one"
                )
            ),
        )
    )
    if expected.get("column"):
        column = str(expected["column"])
        # Two spellings, two boundaries. `_` has to count as a word character, or `duns` matches
        # inside `duns_number` and the case credits a column that was never named. But the same column
        # is also written in prose with spaces - "total obligation" - and for that spelling an
        # underscore is a boundary. So the identifier form and the prose form are matched separately
        # rather than by transforming one into the other.
        named = bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(column)}(?![A-Za-z0-9_])", record)) or bool(
            re.search(rf"(?<![A-Za-z0-9_]){re.escape(column.replace('_', ' '))}(?![A-Za-z0-9_])", record)
        )
        checks.append(
            Check(
                name="names_the_column",
                ok=named,
                expected=column,
                found="named" if named else "not named",
                detail="so a reader can tell which figure was asked for",
            )
        )
    return checks


_GRADERS = {
    "discovery": check_discovery,
    "sequential_lookup": check_sequential,
    "unsigned_join_rejection": check_rejection,
    "relation_trap": check_relation_trap,
    "find_district_rpc": check_rpc,
    "districts_in_bbox_rpc": check_rpc,
    "nonprofits_nearby_rpc": check_rpc,
    "numeric_aggregate": check_numeric,
}


def _used_discover(case_run: dict[str, Any]) -> int:
    return sum(1 for call in tool_calls(case_run) if "discover" in call["name"])


def _requires(case: dict[str, Any], tool: str) -> bool:
    """Whether the case declares a required tool.

    Only enforced when the suite says so. A capability whose cases are written without
    `required_tools` would otherwise fail every case for a tool it never asked for, which reads as a
    broken grader rather than a missing declaration.
    """
    required = case.get("required_tools")
    if not required:
        return False
    return any(tool in str(name) for name in required)


def check_nothing_was_verified(case: dict[str, Any], checks: list[Check]) -> list[Check]:
    """A case whose only checks are negative verified nothing about the answer.

    Reachable when a case declares no `expected` keys and no `required_tools`: the claim checks still
    run and pass, so an unanswered case would grade green. That is the worst failure this grader could
    have, because it is indistinguishable from a pass in a summary table - so it is stated as its own
    check rather than left to the suite's discipline.
    """
    substantive = [check for check in checks if check.name not in {"must_not_claim:unsigned_join_variants"}]
    if substantive:
        return []
    return [
        Check(
            name="verified_something",
            ok=False,
            expected="at least one check about the answer itself",
            found="only negative claim checks ran",
            detail=(
                "this case declares no expected values and no required tools, so grading it would "
                "report a pass having verified nothing. Give it expected values or a required tool"
            ),
        )
    ]


def _was_truncated(case_run: dict[str, Any]) -> bool:
    """Whether the last turn ended because it ran out of token budget."""
    turns = case_run.get("turns") or []
    if not turns:
        return not case_run.get("answer")
    return str(turns[-1].get("finish_reason") or "") == "length"


def grade_case(
    case: dict[str, Any],
    case_run: dict[str, Any],
    signed_relations: set[str] | None = None,
    column_names: set[str] | None = None,
) -> CaseResult:
    """Grade one case. `signed_relations` powers the anti-hallucination floor when supplied."""
    capability = str(case.get("capability") or "")

    # A case the harness could not run is not a case the model failed. The first run recorded a
    # relation_trap failure that was an HTTP 500 and another that was an 8,590-character reasoning turn
    # cut off by a 2,048-token budget, and both read as capability failures. `errored` and `truncated`
    # are infrastructure facts and they are surfaced as their own checks, so a table of capability
    # rates cannot quietly absorb them.
    checks: list[Check] = []
    error = case_run.get("error")
    if error:
        return CaseResult(
            id=str(case.get("id") or ""),
            capability=capability,
            passed=False,
            checks=[
                Check(
                    name="errored",
                    ok=False,
                    expected="the case runs",
                    found=str(error)[:200],
                    detail="the harness could not drive this case; it is not a model failure",
                )
            ],
            question=str(case.get("question") or ""),
            tools_called=[],
        )
    if _was_truncated(case_run):
        return CaseResult(
            id=str(case.get("id") or ""),
            capability=capability,
            passed=False,
            checks=[
                Check(
                    name="truncated",
                    ok=False,
                    expected="the model finishes its turn",
                    found="finish_reason: length",
                    detail=(
                        "the model's turn hit the token budget mid-reasoning, so no answer was "
                        "recorded. That is a harness limit, not a capability failure"
                    ),
                )
            ],
            question=str(case.get("question") or ""),
            tools_called=[],
        )

    grader = _GRADERS.get(capability)
    if grader is not None:
        checks.extend(grader(case, case_run))
    elif "join" in capability:
        checks.extend(check_signed_path(case, case_run))
    else:
        checks.append(
            Check(
                name="known_capability",
                ok=False,
                expected=sorted(_GRADERS),
                found=capability,
                detail="an unknown capability cannot be graded, and is a hole rather than a pass",
            )
        )
    checks.extend(check_forbidden_claims(case, case_run))
    checks.extend(check_nothing_was_verified(case, checks))
    if signed_relations is not None:
        checks.append(check_manifested_relations(case_run, signed_relations, column_names))
    return CaseResult(
        id=str(case.get("id") or ""),
        capability=capability,
        passed=all(check.ok for check in checks),
        checks=checks,
        question=str(case.get("question") or ""),
        tools_called=tools_called(case_run),
    )


def summarise(results: list[CaseResult]) -> dict[str, Any]:
    """Per-capability counts. Deliberately no overall pass rate.

    An aggregate over capabilities of different kinds - exact RPCs beside partly heuristic joins -
    is a number that cannot be acted on. It also invites optimising the suite until the aggregate is
    high, which is how a suite stops measuring anything.
    """
    by_capability: dict[str, dict[str, Any]] = {}
    for result in results:
        bucket = by_capability.setdefault(
            result.capability, {"cases": 0, "passed": 0, "failed_checks": {}, "failed": []}
        )
        bucket["cases"] += 1
        bucket["passed"] += int(result.passed)
        if not result.passed:
            bucket["failed"].append(result.id)
        for check in result.failures():
            bucket["failed_checks"][check.name] = bucket["failed_checks"].get(check.name, 0) + 1
    # Infrastructure outcomes are counted apart from capability rates. A harness failure averaged into
    # a capability rate makes the model look worse than it is, which is the specific misreading that
    # produced a 0/2 for relation_trap that was one 500 and one truncated turn.
    for result in results:
        names = {check.name for check in result.checks}
        if names & {"errored", "truncated"}:
            by_capability.setdefault(result.capability, {"cases": 0, "passed": 0, "failed_checks": {}, "failed": []})
            by_capability[result.capability]["not_measured"] = (
                by_capability[result.capability].get("not_measured", 0) + 1
            )
    for bucket in by_capability.values():
        total = bucket["cases"]
        # Always present, including as zero. A key that only appears when nonzero reads as "not
        # measured" on one capability and as "absent, so zero" on another, which is exactly the kind of
        # ambiguity that made a 0/2 read as a capability failure when it was one 500 and one truncation.
        not_measured = bucket.get("not_measured", 0)
        bucket["not_measured"] = not_measured
        measured = total - not_measured
        bucket["measured"] = measured
        bucket["rate"] = round(bucket["passed"] / measured, 3) if measured else None
        bucket["failed_checks"] = dict(sorted(bucket["failed_checks"].items(), key=lambda kv: -kv[1]))
    return {"cases": len(results), "by_capability": by_capability}


def load_cases(path: str) -> list[dict[str, Any]]:
    document = json.loads(open(path, encoding="utf-8").read())
    if isinstance(document, dict):
        cases = document.get("cases")
        if cases is None:
            raise ValueError(f"{path} has no 'cases' key; keys are {sorted(document)}")
        return list(cases)
    return list(document)
