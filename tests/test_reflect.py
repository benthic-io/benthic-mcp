import re

import pytest
from reflect import (
    assert_no_oracle,
    build_prompt,
    extract_json,
    oracle_only_values,
)

from benthic_mcp.trace import StruggleSignature

SIGNATURE = StruggleSignature(kind="unknown_column", tool="query", detail="Unknown columns for a.b: c", severity=4)

EVENTS = [
    {"name": "benthic_query", "arguments": {"source": "a.b"}, "ok": False, "error": "Unknown columns for a.b: c"},
    {"name": "benthic_query", "arguments": {"source": "a.c"}, "ok": True, "error": None},
]


def test_the_prompt_shows_the_question_calls_and_detected_problem() -> None:
    prompt = build_prompt("which district", EVENTS, "no answer", [SIGNATURE])

    assert "which district" in prompt
    assert "benthic_query" in prompt
    assert "Unknown columns for a.b: c" in prompt
    assert "no answer" in prompt


def test_a_session_with_no_tool_calls_renders_a_placeholder_once() -> None:
    prompt = build_prompt("q", [], "answer", [SIGNATURE])

    assert prompt.count("(none)") == 1


def test_a_missing_final_answer_is_stated_plainly() -> None:
    prompt = build_prompt("q", EVENTS, "", [SIGNATURE])

    assert "never produced an answer" in prompt


def test_the_prompt_never_contains_oracle_material() -> None:
    prompt = build_prompt("q", EVENTS, "answer", [SIGNATURE])

    for marker in ('"expected"', "right_keys", "left_key", "bound_value", "context_value", "right_count"):
        assert marker not in prompt


@pytest.mark.parametrize(
    "leak",
    ['{"expected": {"rows": []}}', "'expected'", '"right_keys": ["a"]', '"left_key": "x"', '"bound_value": "5"'],
)
def test_the_oracle_guard_catches_a_dumped_oracle_document(leak: str) -> None:
    with pytest.raises(ValueError):
        assert_no_oracle(leak)


def test_a_model_choosing_the_same_words_for_its_own_filter_is_not_a_leak() -> None:
    # The agent guessed `left_key=142362594` as a context_conditions entry. Unquoted filter
    # syntax is the model's own, not the oracle's.
    assert_no_oracle("context_conditions: [left_key=142362594, duns=ein]")


def test_oracle_only_values_are_excluded_when_the_agent_already_saw_them() -> None:
    expected = {"left_key": "142362594", "right_keys": ["9988776655"]}
    legitimate = "the agent queried uei eq.142362594 and saw nothing"

    assert oracle_only_values(expected, legitimate) == {"9988776655"}


def test_a_real_leak_is_caught_by_the_value_test() -> None:
    # The harness filters oracle values against what the agent already saw, then checks whether
    # any survivor appears in the prompt. A key the agent never retrieved cannot appear there.
    expected = {"right_keys": ["9988776655"]}
    legitimate = "the agent queried duns eq.142362594 and got an error"

    forbidden = oracle_only_values(expected, legitimate)
    prompt = "the answer is EIN 9988776655"

    assert forbidden == {"9988776655"}
    assert forbidden & set(re.findall(r"[A-Za-z0-9._-]{4,}", prompt)) == {"9988776655"}


def test_short_numerics_are_ignored_because_they_collide_with_ordinary_output() -> None:
    assert oracle_only_values({"right_count": 7}, "rows 7 of 7 returned") == set()


def test_nested_join_path_structure_is_not_treated_as_a_leak() -> None:
    expected = {"join_path": {"left": "a.b", "right": "c.d", "left_column": "uei"}}

    assert oracle_only_values(expected, "joined a.b to c.d on uei") == set()


def test_row_count_in_agent_output_is_not_treated_as_a_leak() -> None:
    # row_count is part of every RpcResult, so flagging it would fail every spatial case.
    assert_no_oracle("the RPC returned row_count=12")


def test_fenced_json_is_recovered() -> None:
    assert extract_json('```json\n{"lesson": "a"}\n```') == {"lesson": "a"}


def test_json_embedded_in_prose_is_recovered() -> None:
    assert extract_json('Here you go: {"lesson": "a", "symptom": "b"} hope that helps') == {
        "lesson": "a",
        "symptom": "b",
    }


def test_unparsable_output_returns_none() -> None:
    assert extract_json("I am not going to answer in JSON") is None
    assert extract_json("") is None
