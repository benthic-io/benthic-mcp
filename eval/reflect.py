"""The reflection turn: turn a detected struggle into a lesson, using the calling model.

Voluntary self-reporting measured at 0% uptake, so the loop had no input. Instead the server
detects objective signs that a session went wrong and this module asks the same model, which
just failed, what a future session should do differently.

ORACLE ISOLATION IS A HARD RULE. The reflector may see the question, the tool calls, the
arguments, the errors, and the agent's own final answer. It may never see `case["expected"]`,
which contains the literal answer (`rows`, `row_count`, `right_keys`, `left_key`, `bound_value`).
If it could, every "lesson" would be the answer restated and the loop would be reading the
answer key. `build_prompt` is the only place the user text is assembled, and `assert_no_oracle`
guards it.
"""

import json
from typing import Any

import httpx

from benthic_mcp.trace import StruggleSignature

SYSTEM_PROMPT = """You are reviewing a session in which an analyst agent struggled against a read-only data MCP.

You will see the question, the tool calls the agent made, any errors, and its final answer. You
will NOT see the correct answer, because you do not have it. Do not guess what the answer was.

Write one short lesson that would help a different agent on a similar question. The lesson is
about method, not about this question's answer.

Rules:
1. Only name a dataset, relation, or column that appears in the tool output above. Never invent one.
2. Do not state or imply the correct answer to the question. You do not know it.
3. Do not suggest a join unless a signed join path appears in the output.
4. Write the lesson as an imperative instruction, one or two sentences.
5. If the agent actually handled the question well, return an empty lesson.
6. Set "dataset" only when the correction would be wrong on some other dataset. Mistakes about
   method, verification, error handling, or reading result flags are almost never dataset-specific,
   so leave "dataset" null for those. Scoping to the dataset of the failing case is the most common
   way to write a lesson that then gets duplicated once per dataset.
7. Set "relation" only when the correction names one specific relation and would be wrong elsewhere.

Return only JSON: {"symptom": string, "lesson": string, "dataset": string|null,
"relation": string|null, "confidence": "high"|"medium"|"low"}."""

# Second line of defence. These are checked in their quoted JSON form on purpose: the agent's own
# filter strings look like `left_key=142362594`, unquoted, and it may legitimately choose the same
# words for its own arguments. Only dumping the oracle dict would produce the quoted form.
_ORACLE_MARKERS = (
    '"expected"',
    "'expected'",
    '"right_keys"',
    '"left_key"',
    '"bound_value"',
    '"context_value"',
    '"right_count"',
)


def assert_no_oracle(text: str) -> None:
    """Fail loudly if the oracle document itself was interpolated into the prompt."""
    for marker in _ORACLE_MARKERS:
        if marker in text:
            raise ValueError(f"reflection prompt leaks oracle field {marker}")


def oracle_only_values(expected: dict[str, Any], legitimate: str) -> set[str]:
    """Oracle values that appear nowhere in the question, tool calls, or final answer.

    This is the precise check. Field names are unreliable because a model may pick the same words
    for its own arguments, but a value that exists only in `expected` cannot have been derived from
    what the agent saw. Short numerics are ignored because they collide with ordinary output.
    """
    tokens: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            if abs(value) >= 100:
                tokens.add(str(value))
            return
        if isinstance(value, str):
            if len(value) >= 4:
                tokens.add(value)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(expected)
    return {token for token in tokens if token not in legitimate}


def assert_no_oracle_values(text: str, expected: dict[str, Any]) -> None:
    leaked = oracle_only_values(expected, text)
    if leaked:
        sample = sorted(leaked)[:3]
        raise ValueError(f"reflection prompt leaks oracle values {sample}")


def _format_call(event: dict[str, Any]) -> str:
    name = event.get("name", "?")
    arguments = {key: value for key, value in (event.get("arguments") or {}).items() if value is not None}
    if event.get("ok"):
        return f"  {name}({json.dumps(arguments, sort_keys=True, default=str)}) -> ok"
    return f"  {name}({json.dumps(arguments, sort_keys=True, default=str)}) -> ERROR: {str(event.get('error'))[:400]}"


def build_prompt(
    question: str,
    events: list[dict[str, Any]],
    final_text: str,
    signatures: list[StruggleSignature],
) -> str:
    calls = [_format_call(event) for event in events] or ["  (none)"]
    lines = [
        "QUESTION THE AGENT WAS ASKED:",
        question[:1000],
        "",
        "TOOL CALLS THE AGENT MADE, IN ORDER:",
        *calls,
        "",
        "WHAT WENT WRONG (detected by the server, not by the agent):",
    ]
    lines.extend(f"- {signature.kind} [{signature.tool}]: {signature.detail[:300]}" for signature in signatures)
    lines += ["", "THE AGENT'S FINAL ANSWER:", (final_text[:1500] or "(the agent never produced an answer)")]
    lines += ["", "Write one lesson for a future agent."]
    prompt = "\n".join(lines)
    assert_no_oracle(prompt)
    return prompt


def extract_json(raw: str) -> dict[str, Any] | None:
    fence = "```"
    candidates = [raw]
    if fence in raw:
        start = raw.find(fence)
        body = raw[start + len(fence) :]
        body = body[3:] if body.lower().startswith("json") else body
        if fence in body:
            candidates.append(body.split(fence)[0])
    first, last = raw.find("{"), raw.rfind("}")
    if first != -1 and last > first:
        candidates.append(raw[first : last + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


async def reflect(
    base_url: str,
    model: str,
    question: str,
    events: list[dict[str, Any]],
    final_text: str,
    signatures: list[StruggleSignature],
    forbidden: set[str] | None = None,
    timeout: float = 240.0,
) -> dict[str, Any] | None:
    """Ask the calling model what a future session should do differently.

    `forbidden` carries values derived from the case oracle by the caller, so this module never
    receives the oracle itself.     Returns (proposal, reason). `reason` is "ok" on success, otherwise why nothing was recorded,
    so a low yield is diagnosable instead of mysterious.
    """
    prompt = build_prompt(question, events, final_text, signatures)
    if forbidden:
        leaked = sorted(value for value in forbidden if value in prompt)
        if leaked:
            raise ValueError(f"reflection prompt leaks oracle values {leaked[:3]}")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2500,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{base_url.rstrip('/')}/v1/chat/completions", json=payload)
        response.raise_for_status()
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        return None, "no_choices"
    choice = choices[0]
    message = choice.get("message", {})
    raw = str(message.get("content") or "").strip()
    finish = str(choice.get("finish_reason") or "unknown")
    if not raw:
        # Reasoning models can spend the whole budget thinking and emit no answer.
        return None, f"empty_content(finish={finish})"
    parsed = extract_json(raw)
    if not parsed:
        return None, f"unparsable(finish={finish}, {len(raw)} chars)"

    lesson = str(parsed.get("lesson") or "").strip()
    symptom = str(parsed.get("symptom") or "").strip()
    if not lesson or not symptom:
        return None, f"declined_or_incomplete(finish={finish})"
    confidence = str(parsed.get("confidence") or "medium").lower()
    return {
        "symptom": symptom[:500],
        "lesson": lesson[:500],
        "dataset": str(parsed["dataset"]) if parsed.get("dataset") else None,
        "relation": str(parsed["relation"]) if parsed.get("relation") else None,
        "confidence": confidence if confidence in {"high", "medium", "low"} else "medium",
    }, "ok"
