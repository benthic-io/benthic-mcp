#!/usr/bin/env python3
"""Run a set of probes through the live chat interface and record every turn.

Each probe is one question driven through the loop the web UI implements. Every assistant turn is
recorded verbatim, because the reasoning is the artifact under test, not just the final answer.

Usage:
    sweep.py --probes probes/basic.json --record out/records.jsonl [--max-turns 6]
    sweep.py --probes probes/basic.json --only discover_join --record ...
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("BENTHIC_LLAMA_URL", "http://192.168.10.222:8081")


def post(path: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get(path: str, timeout: float) -> object:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def call_tool(name: str, arguments: dict, timeout: float) -> dict:
    """Execute one tool. llama-server normalises the MCP result to plain_text_response."""
    raw = post("/tools", {"tool": name, "params": arguments}, timeout)
    text = raw.get("plain_text_response")
    if text is None:
        return {"ok": False, "text": json.dumps(raw)}
    return {"ok": True, "text": text}


TOOLS_CACHE: list | None = None


def tools() -> list:
    global TOOLS_CACHE
    if TOOLS_CACHE is None:
        TOOLS_CACHE = [tool["definition"] for tool in get("/tools", 30)]
    return TOOLS_CACHE


def run_probe(probe: dict, max_turns: int, timeout: float, temperature: float) -> dict:
    question = probe["question"]
    messages: list[dict] = [{"role": "user", "content": question}]
    turns: list[dict] = []
    started = time.monotonic()
    errors: list[str] = []

    for turn_number in range(1, max_turns + 1):
        try:
            response = post(
                "/v1/chat/completions",
                {
                    "model": "local",
                    "messages": messages,
                    "tools": tools(),
                    # This model reasons before it answers, and a long reasoning turn can exhaust a
                    # small budget on its own: at 1400 tokens one turn spent all 1,400 on reasoning
                    # and emitted nothing, which reads identically to a refusal. Budget for thinking.
                    "max_tokens": probe.get("max_tokens", 4000),
                    "temperature": temperature,
                    "top_p": 0.95,
                },
                timeout,
            )
        except Exception as exc:  # noqa: BLE001 - a transport failure is a recorded outcome
            errors.append(f"turn {turn_number}: {type(exc).__name__}: {exc}")
            break

        if "error" in response:
            errors.append(f"turn {turn_number}: {json.dumps(response['error'])[:500]}")
            break

        choice = response["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
        # `--reasoning-preserve` is set on this server, so a thinking turn puts its output here and
        # leaves `content` empty. Recording only `content` made 61% of turns look blank and a turn
        # that ran out of budget mid-thought look like an empty `finish_reason: length`.
        reasoning = message.get("reasoning_content") or ""
        tool_calls = message.get("tool_calls") or []

        record = {
            "turn": turn_number,
            "content": content,
            "reasoning": reasoning,
            "tool_calls": [
                {"name": call["function"]["name"], "arguments": call["function"]["arguments"]} for call in tool_calls
            ],
            "finish_reason": choice.get("finish_reason"),
            "usage": response.get("usage"),
        }
        turns.append(record)

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        if not tool_calls:
            break

        for call in tool_calls:
            function = call["function"]
            try:
                arguments = json.loads(function["arguments"])
            except json.JSONDecodeError as exc:
                # The model can emit a tool call whose arguments are truncated mid-JSON, which is
                # what a `finish_reason: length` on the tool-calling turn looks like from here.
                # Sending the raw string on as params produced a 500 from the server and lost the
                # turn entirely; recorded as a broken call instead, so the transcript still shows
                # what the model tried.
                record.setdefault("broken_calls", []).append(
                    {
                        "name": function["name"],
                        "raw_arguments": function["arguments"][:600],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if not isinstance(arguments, dict):
                record.setdefault("broken_calls", []).append(
                    {"name": function["name"], "raw_arguments": str(arguments)[:600], "error": "not an object"}
                )
                continue
            result = call_tool(function["name"], arguments, timeout)
            record.setdefault("tool_results", []).append(
                {"name": function["name"], "ok": result["ok"], "text": result["text"][:6000]}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", "call_0"),
                    "name": function["name"],
                    "content": result["text"],
                }
            )

    return {
        "id": probe.get("id", question[:40]),
        "cluster": probe.get("cluster", "unclassified"),
        "expect": probe.get("expect", ""),
        "question": question,
        "turns": turns,
        "answered": bool(turns and turns[-1]["tool_calls"] == [] and turns[-1]["content"].strip()),
        "exhausted_turns": len(turns) >= max_turns and bool(turns and turns[-1]["tool_calls"]),
        "errors": errors,
        "elapsed_s": round(time.monotonic() - started, 1),
        "tool_call_count": sum(len(t["tool_calls"]) for t in turns),
        "final_text": turns[-1]["content"] if turns and not turns[-1]["tool_calls"] else "",
        "final_reasoning": turns[-1].get("reasoning", "") if turns else "",
        # A turn that only thought is not an answer, but it is not a blank one either. Truncated
        # or exhausted is a distinct outcome from answered, and conflating them hides the case where
        # the model did the work and ran out of budget before writing it down.
        "thought_only_last_turn": bool(turns and not turns[-1]["tool_calls"] and not turns[-1]["content"].strip()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probes", required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("--only", default="", help="comma-separated probe ids or clusters")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--temperature", type=float, default=0.0)
    args = parser.parse_args()

    probes = json.loads(Path(args.probes).read_text(encoding="utf-8"))["probes"]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",")}
        probes = [p for p in probes if p.get("id") in wanted or p.get("cluster") in wanted]

    out = Path(args.record)
    out.parent.mkdir(parents=True, exist_ok=True)
    total = len(probes)
    for index, probe in enumerate(probes, 1):
        print(f"[{index}/{total}] {probe.get('id')} ({probe.get('cluster')})", flush=True)
        result = run_probe(probe, args.max_turns, args.timeout, args.temperature)
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, default=str) + "\n")
        flags = []
        if not result["answered"]:
            flags.append("UNANSWERED")
        if result["errors"]:
            flags.append(f"ERRORS={len(result['errors'])}")
        if result["exhausted_turns"]:
            flags.append("TURN-EXHAUSTED")
        print(
            f"    answered={result['answered']} calls={result['tool_call_count']} "
            f"turns={len(result['turns'])} {result['elapsed_s']}s {' '.join(flags)}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
