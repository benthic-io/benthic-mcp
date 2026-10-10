"""Rewordings that must not change a verdict.

The nine grader defects found on 2026-10-09 shared one shape: a check read the wording it expected
rather than the wording models produce. `"$1,234.56"` became four numbers; a fifteen-digit count was
graded as unstated because it was written "zero"; a sentence boundary was detected inside
`usp_cl.mv_current_lawmakers`; a rejection in the next sentence read as reliance; a column alias read as
an invented table. Every one was found by reading a specific failure that happened to surface.

Each of these transformations is meaning-preserving by construction, and each one turns a class of that
defect into a verdict change. Running them over the stored transcripts asks the question no
individual contract asked: **does this grader grade the answer that was written, or the answer I
imagined?**

`tests/test_paraphrase_invariance.py` drives them. See `items()` there for what each one is for.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# A number token, integer or decimal, with optional currency and grouping.
_NUMBER = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?")

# An identifier, optionally dataset-qualified. Snake case is what the manifest uses, and it is also
# what a model wraps in backticks - both spellings have to read the same.
# An identifier, optionally dataset-qualified. Greedy over the whole run so that
# `usaspending.all_entities` is one token: matching the segments separately produced
# ``usaspending.`all_entities` ``, which no model writes and which broke `_mentions` for every
# qualified relation. The word filter is applied after the match, so the pattern itself stays simple.
_IDENTIFIER = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)*\b")

_WHITESPACE = re.compile(r"\s+")


def whitespace(text: str, asserted: set[str]) -> str:
    """Collapse runs of blanks and trim. Trivially meaning-preserving.

    A grader that splits on a blank run, or that trims inconsistently, changes verdict on this.
    """
    return _WHITESPACE.sub(" ", text).strip()


def backtick_identifiers(text: str, asserted: set[str]) -> str:
    """Wrap every snake-case identifier in backticks: `usp_cl.mv_current_lawmakers`.

    Models write both, sometimes in the same answer. This exercises the boundary classes in
    `_mentions` and in the trap window: `find(".")` matching the period inside the identifier closed
    the window before the mention was over, and a backticked mention must behave like a bare one.
    """
    # Greedy over the whole run so `usaspending.all_entities` is one token, then filtered to runs
    # that are actually identifiers. Matching the segments separately produced
    # ``usaspending.`all_entities` ``, which no model writes and which broke `_mentions`.
    return _IDENTIFIER.sub(
        lambda m: f"`{m.group(0)}`" if "_" in m.group(0) and m.group(0) not in asserted else m.group(0),
        text,
    )


TAIL = " That is the answer to the question as asked."


def trailing_sentence(text: str, asserted: set[str]) -> str:
    """Append a sentence containing no number, identifier, marker or trap name.

    A rejection's reach is decided by how close the next sentence is, so appending one probes whether
    a window reads past the end of what was said. The appended text is chosen to contain none of the
    things any check looks for, so it cannot change a verdict on its own.
    """
    return text.rstrip() + TAIL


def group_numbers(text: str, asserted: set[str]) -> str:
    """Write every integer with thousands separators, and prefix money with `$`.

    `1234` -> `$1,234`. Exactly the same number, so a count or a total must be read identically. The
    original defect here read `"$1,234.56"` as four numbers, so a grader that fragments a grouped
    figure - or that demands one spelling - changes verdict on this.
    """

    def rewrite(match: re.Match[str]) -> str:
        raw = match.group(0)
        negative = raw.startswith("-")
        digits = raw.lstrip("-$")
        currency = "$" in raw
        if "." in digits:
            whole, _, fraction = digits.partition(".")
            if not whole.isdigit() or not fraction.isdigit():
                return raw
            body = f"{int(whole):,}.{fraction}"
        else:
            if not digits.isdigit():
                return raw
            body = f"{int(digits):,}"
        if currency or len(body.replace(",", "").split(".")[0]) >= 5:
            return f"{'-' if negative else ''}${body}"
        return f"{'-' if negative else ''}{body}"

    if not asserted:
        return _NUMBER.sub(rewrite, text)
    # A numeric UEI is digit-shaped but it is an identifier. Rewriting `142362594` as `142,362,594`
    # changes the value `reports:left_key` looks for, and the verdict change it caused said nothing
    # about the grader.
    pattern = "|".join(re.escape(value) for value in sorted(asserted, key=len, reverse=True))
    guarded = re.compile(rf"(?<![A-Za-z0-9])(?:{pattern})(?![A-Za-z0-9])")
    out: list[str] = []
    position = 0
    for match in guarded.finditer(text):
        out.append(_NUMBER.sub(rewrite, text[position : match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(_NUMBER.sub(rewrite, text[position:]))
    return "".join(out)


def prose_columns(text: str, asserted: set[str]) -> str:
    """The case's expected column, written the way a person writes it in a sentence.

    Restricted to the asserted column. Rewriting every identifier would also rewrite the relations the
    case requires and the trap it requires to be named, so the transform would be changing the target
    rather than the wording - which is what the unmanifested_relation and did_not_answer_from_trap flips
    were, before this restriction.

    Within that restriction it asks a real question: does the discovery check credit a column written
    as "total obligation" as well as `total_obligation`? The numeric column check has a prose form; this
    finds out whether the others do.
    """
    for column in sorted(asserted, key=len, reverse=True):
        # A relation is `dataset.relation` and a trap name is too. Neither is a column, and rewriting
        # one changes what the case requires to be named.
        if "_" not in column or "." in column:
            continue
        text = re.sub(rf"(?<![A-Za-z0-9_.]){re.escape(column)}(?![A-Za-z0-9_])", column.replace("_", " "), text)
    return text


#: Each transform, in the order the corpus runs them. The comment is what it is for.
TRANSFORMS: tuple[tuple[str, Callable[[str, set[str]], str]], ...] = (
    ("whitespace", whitespace),
    ("backtick_identifiers", backtick_identifiers),
    ("trailing_sentence", trailing_sentence),
    ("group_numbers", group_numbers),
    ("prose_columns", prose_columns),
)


def asserted_values(case: dict) -> set[str]:
    """Every value the case's own checks require to appear verbatim.

    A rewording must not change what a case asserts on. Rewriting an asserted value changes the target,
    not the wording, and a verdict change it causes says nothing about the grader. `{"left_key":
    "142362594"}` is the case that revealed this: a numeric UEI is digit-shaped but it is an identifier,
    and rewriting it with thousands separators changes the thing `reports:left_key` looks for.
    """
    out: set[str] = set()
    for value in (case.get("expected") or {}).values():
        if isinstance(value, str) and value:
            out.add(value)
        elif isinstance(value, dict):
            out.update(v for v in value.values() if isinstance(v, str))
    return out


def transform_answer(run: dict, name: str, case: dict) -> dict:
    """Apply one transform to every string field of a recorded run, leaving structure intact.

    `answer_text` joins the final answer with each turn's reasoning, so both are rewording surfaces.
    The structure - ids, tool names, tool results - is untouched, because those are what the
    capability checks read and a rewording must not move them.
    """
    apply = dict(TRANSFORMS)[name]
    skip = asserted_values(case)
    rewritten = json_copy(run)
    if isinstance(rewritten.get("answer"), str):
        rewritten["answer"] = apply(rewritten["answer"], skip)
    for turn in rewritten.get("turns") or []:
        if isinstance(turn, dict):
            for key in ("reasoning", "content"):
                if isinstance(turn.get(key), str):
                    turn[key] = apply(turn[key], skip)
    return rewritten


def json_copy(run: dict) -> dict:
    import json

    return json.loads(json.dumps(run))
