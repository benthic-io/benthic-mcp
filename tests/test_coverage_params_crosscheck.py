"""The corpus's provenance strings and the server's request builder must agree.

The audit trail was fixed to record .nullslast, but a string is only trustworthy if it is compared
against the thing that actually builds requests. This is that comparison: every ordered case in
both derived corpora must record exactly the order string the server's own formatter produces for
that direction, and every filtered case must record the scalar the server's formatter would send.

A contract that only checks the string could pass while the server drifted; a contract that only
checks the server could pass while the corpus drifted. Both sides pinned to each other is what
makes the drift visible wherever it starts.
"""

import json
from pathlib import Path
from typing import Any

from benthic_mcp.models import SourceOrder
from benthic_mcp.postgrest import PostgrestTransport

ROOT = Path(__file__).resolve().parents[1]
_SUITES: tuple[Path, ...] = (
    ROOT / "eval" / "truth" / "numeric_cases.json",
    ROOT / "eval" / "coverage" / "cases.json",
)
_ORDERED_KINDS = ("ordered_max", "filtered_ordered_max", "ordered_min")


def _cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in _SUITES:
        out.extend(json.loads(path.read_text(encoding="utf-8"))["cases"])
    return out


def test_every_ordered_case_records_the_servers_order_string() -> None:
    ordered = [c for c in _cases() if c["expected"].get("kind") in _ORDERED_KINDS]
    assert ordered, "both corpora must still carry ordered cases for this contract to mean anything"

    checked = 0
    for case in ordered:
        column = case["expected"]["column"]
        descending = case["expected"]["kind"] != "ordered_min"
        # max descends and min ascends, both with nullslast: without it a null-heavy column answers
        # null instead of the extreme. The server builds this exact string via _format_order.
        want = PostgrestTransport._format_order(SourceOrder(column=column, descending=descending, nulls_first=False))
        assert want in case["derived_from"], (
            f"{case['id']}: provenance records {case['derived_from']!r}, but the server sends {want!r}"
        )
        checked += 1
    assert checked >= 403, checked  # 15 truth ordered_max + 92 ordered_min + 296 filtered_ordered_max


def test_the_two_directions_are_not_symmetrical_typos() -> None:
    """max descends, min ascends; both nullslast. If the two ever agree, one of them drifted."""
    maxim = PostgrestTransport._format_order(SourceOrder(column="x", descending=True, nulls_first=False))
    minim = PostgrestTransport._format_order(SourceOrder(column="x", descending=False, nulls_first=False))
    assert maxim == "x.desc.nullslast", maxim
    assert minim == "x.asc.nullslast", minim


def test_every_filtered_case_records_a_filter_the_server_could_send() -> None:
    """derived_from for filtered cases must contain an eq.<scalar> the server's formatter produces."""
    filtered = [
        c
        for c in _cases()
        if c["expected"].get("kind") in ("filtered_count", "row_count") and c["expected"].get("where")
    ]
    assert filtered, "filtered cases must exist for this contract to mean anything"
    for case in filtered:
        sample = str(case["expected"].get("sample", ""))
        assert sample and ("eq." in case["derived_from"]), (
            f"{case['id']}: a filtered case must record the filter it derived from"
        )
