"""The harness must not feed the model a result's first N characters and nothing else.

Measured on the 618-case counts slice: 228 of 618 cases held at least one tool result over the
6000-character cap, and the result envelope carries `row_count`, `truncated` and `warnings` after
the rows. A head-only cut showed the model rows with no total and no warning, and the slice's two
worst loops were exactly those cases: `usaspending.frec_map` fetched the same first page eight times
with its warning cut away, and `form990_soi.compensation_officers` six times. The server emitted the
information that stops the loop; the runner ate it.

A trim that keeps the tail keeps that information. These contracts pin the behaviour.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval" / "truth"))

from run_suite import trim_for_model  # noqa: E402  # type: ignore[import-not-found]


def _envelope(row_count: int = 100) -> str:
    rows = [{"id": i, "agency_identifier": "000", "main_account_code": "0400"} for i in range(row_count)]
    payload = {
        "columns": ["id", "agency_identifier", "main_account_code"],
        "rows": rows,
        "row_count": row_count,
        "source_complete": False,
        "truncated": True,
        "warnings": [
            {
                "source": "s",
                "message": (
                    f"usaspending.frec_map returned {row_count} of 13464 rows that match the filters. "
                    "Paging cannot produce an answer at this size, so answer now with the rows held "
                    "and state that the result is a partial view rather than a total."
                ),
            }
        ],
    }
    return json.dumps(payload, indent=2)


def test_a_result_under_the_cap_is_returned_unchanged() -> None:
    text = _envelope(3)
    assert len(text) <= 6000
    assert trim_for_model(text) == text


def test_a_result_over_the_cap_still_ends_with_its_own_tail() -> None:
    text = _envelope(100)
    assert len(text) > 6000
    trimmed = trim_for_model(text)
    assert len(trimmed) <= 6000
    assert trimmed.startswith(text[:100])
    # The tail is the point: the envelope after the rows is what the model needs to stop looping.
    assert trimmed.endswith(text[-100:])
    assert '"warnings"' in trimmed
    assert '"row_count"' in trimmed
    assert '"truncated"' in trimmed
    assert "13464" in trimmed


def test_the_cut_says_that_it_happened_and_where_the_metadata_lives() -> None:
    trimmed = trim_for_model(_envelope(100))
    assert "cut" in trimmed
    marker = trimmed[trimmed.index("...") : trimmed.index("...") + 220]
    assert "row_count" in marker


def test_a_trim_never_grows_a_result() -> None:
    text = "x" * 9001
    assert len(trim_for_model(text)) <= 6000
    assert len(trim_for_model(text, limit=100)) <= 100
