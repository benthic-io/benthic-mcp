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


def check_manifested_relations(case_run: dict[str, Any], signed_relations: set[str]) -> Check:
    """No relation the model named may be absent from the signed manifest.

    This is the anti-hallucination floor and it is the check that would catch a model inventing a
    table to make an answer work. It is deliberately narrow: only `dataset.relation` pairs the model
    wrote in prose are considered, because guessing at relation-shaped words is not this check's job.
    """
    text = answer_text(case_run)
    invented = sorted(
        spelling
        for spelling in _relation_spellings(text)
        if _looks_like_a_relation(spelling) and spelling.replace("`", "").lower() not in signed_relations
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


def _looks_like_a_relation(spelling: str) -> bool:
    """Whether a dotted spelling is a relation reference rather than prose or a fragment.

    Both halves must be plausible identifiers: a relation in this catalog is written out in full, and a
    one-or-two character half is a fragment of a longer word rather than a name.
    """
    dataset, _, relation = spelling.partition(".")
    if dataset.lower() in _NOT_A_RELATION or relation.lower() in _NOT_A_RELATION:
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
        used_trap = _mentions(text, trap)
        checks.append(
            Check(
                name="did_not_answer_from_trap",
                ok=not used_trap,
                expected=f"no reliance on {trap}",
                found="trap named" if used_trap else "not named",
                detail=f"{trap} cannot answer a historical question" if used_trap else "",
            )
        )
    return checks


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


_GRADERS = {
    "discovery": check_discovery,
    "sequential_lookup": check_sequential,
    "unsigned_join_rejection": check_rejection,
    "relation_trap": check_relation_trap,
    "find_district_rpc": check_rpc,
    "districts_in_bbox_rpc": check_rpc,
    "nonprofits_nearby_rpc": check_rpc,
    # The `_limits` variants differ from their base case only in what the question asks the model to
    # say about the result, and their `expected` block is the base case's. Registering them is better
    # than letting them fall through to `known_capability`: a capability with no grader must be a loud
    # failure, not a scored hole that looks like a model problem.
    "find_district_rpc_limits": check_rpc,
    "districts_in_bbox_rpc_limits": check_rpc,
    "nonprofits_nearby_rpc_limits": check_rpc,
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


def grade_case(case: dict[str, Any], case_run: dict[str, Any], signed_relations: set[str] | None = None) -> CaseResult:
    """Grade one case. `signed_relations` powers the anti-hallucination floor when supplied."""
    capability = str(case.get("capability") or "")
    grader = _GRADERS.get(capability)
    checks: list[Check] = []
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
        checks.append(check_manifested_relations(case_run, signed_relations))
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
    for bucket in by_capability.values():
        total = bucket["cases"]
        bucket["rate"] = round(bucket["passed"] / total, 3) if total else None
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
