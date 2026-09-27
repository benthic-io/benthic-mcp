import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).parents[1]

# The per-case progress line is the signal when watching a long run; the transport chatter is not.
for noisy in ("httpx", "httpcore", "mcp", "mcp.server"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def read_token() -> str:
    token = os.environ.get("BENTHIC_MCP_BEARER_TOKEN")
    if token:
        return token
    env_path = Path.home() / ".config" / "benthic-mcp" / "env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("BENTHIC_MCP_BEARER_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("BENTHIC_MCP_BEARER_TOKEN is not configured")


class AuthTransport:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token

    async def __aenter__(self) -> tuple[Any, Any]:
        self.http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=180.0,
        )
        self.context = streamable_http_client(self.url, http_client=self.http)
        return await self.context.__aenter__()

    async def __aexit__(self, *args: Any) -> Any:
        try:
            return await self.context.__aexit__(*args)
        finally:
            await self.http.aclose()


async def model_id(client: httpx.AsyncClient, base_url: str, configured: str | None) -> str:
    if configured:
        return configured
    response = await client.get(f"{base_url}/v1/models")
    response.raise_for_status()
    data = response.json()
    models = data.get("data", [])
    if not models or not models[0].get("id"):
        raise RuntimeError("The local llama-server returned no model")
    return str(models[0]["id"])


def tool_definitions(tools: list[Any]) -> list[dict[str, Any]]:
    definitions = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema or {"type": "object", "properties": {}},
                },
            }
        )
    return definitions


def parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def result_payload(result: Any) -> tuple[str, dict[str, Any] | None, bool]:
    structured = getattr(result, "structured_content", None)
    content = getattr(result, "content", None) or []
    text = "\n".join(str(item.text) for item in content if getattr(item, "text", None))
    if not text and structured is not None:
        text = json.dumps(structured, sort_keys=True, ensure_ascii=True)
    return text, structured if isinstance(structured, dict) else None, bool(getattr(result, "is_error", False))


def call_key(name: str, arguments: dict[str, Any]) -> str:
    return name + ":" + json.dumps(arguments, sort_keys=True, separators=(",", ":"))


def contains_any(text: str, values: list[str]) -> bool:
    lowered = text.lower()
    return any(value.lower() in lowered for value in values)


def discovered_relations(events: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Qualified sources and bare relation names the agent actually retrieved."""
    qualified: set[str] = set()
    bare: set[str] = set()
    for event in events:
        structured = event.get("structured") or {}
        for entry in structured.get("relations", []) or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("source"):
                qualified.add(str(entry["source"]))
            if entry.get("relation"):
                bare.add(str(entry["relation"]))
    return qualified, bare


def relation_was_discovered(qualified: set[str], bare: set[str], expected: str) -> bool:
    """Cases store a qualified name like `usaspending.agency`, while DiscoverResult reports the
    bare name in `relation` and the qualified one in `source`. Comparing the qualified expectation
    against bare names alone silently failed every discovery case."""
    expected = str(expected)
    if expected in qualified:
        return True
    return expected.rsplit(".", 1)[-1] in bare


def _join_matches(event: dict[str, Any], path: dict[str, Any]) -> bool:
    arguments = event.get("arguments", {})
    forward = (
        arguments.get("left_source") == path.get("left")
        and arguments.get("left_column") == path.get("left_column")
        and arguments.get("right_source") == path.get("right")
        and arguments.get("right_column") == path.get("right_column")
    )
    reverse = (
        arguments.get("left_source") == path.get("right")
        and arguments.get("left_column") == path.get("right_column")
        and arguments.get("right_source") == path.get("left")
        and arguments.get("right_column") == path.get("left_column")
    )
    return forward or reverse


def queried_sources(events: list[dict[str, Any]]) -> set[str]:
    return {
        str(event.get("arguments", {}).get("source"))
        for event in events
        if event.get("name") == "benthic_query" and event.get("ok")
    }


def score_case(
    case: dict[str, Any],
    events: list[dict[str, Any]],
    final_text: str,
    error: str = "",
    strict: bool = False,
) -> dict[str, Any]:
    successful = [event for event in events if event["ok"]]
    names = {event["name"] for event in successful}
    required = set(case.get("required_tools", []))
    tool_requirement = required.issubset(names)
    expected = case.get("expected", {})
    capability = case.get("capability", "")
    join_ok = True
    row_count_ok = True
    evidence_ok = True
    answered = True

    if strict:
        # A run that exhausts its turns without producing an answer has delivered nothing, even
        # if every required tool was called. The legacy scorer lets that count as a pass.
        answered = bool(final_text.strip()) and error != "maximum turns reached"

    if "join" in capability and capability != "unsigned_join_rejection":
        matching = [event for event in successful if event["name"] == "benthic_join"]
        join_ok = bool(matching)
        if join_ok and expected.get("join_path"):
            path = expected["join_path"]
            join_ok = any(
                event.get("arguments", {}).get("left_source") == path.get("left")
                and event.get("arguments", {}).get("right_source") == path.get("right")
                and event.get("arguments", {}).get("left_column") == path.get("left_column")
                and event.get("arguments", {}).get("right_column") == path.get("right_column")
                for event in matching
            )
        evidence_ok = not contains_any(final_text, ["unsigned", "not signed"]) or "reliable" in final_text.lower()

    if (
        capability.startswith("find_district")
        or capability.startswith("districts_in_bbox")
        or capability.startswith("nonprofits_nearby")
    ):
        rpc_events = [event for event in successful if event["name"] == "benthic_rpc"]
        row_count_ok = any(
            event.get("structured", {}).get("row_count") == expected.get("row_count") for event in rpc_events
        )

    if capability == "sequential_lookup":
        query_sources = {
            event.get("arguments", {}).get("source") for event in successful if event["name"] == "benthic_query"
        }
        join_ok = {expected.get("left"), expected.get("right")}.issubset(query_sources)

    if capability == "unsigned_join_rejection":
        invalid_join = any(event["name"] == "benthic_join" and event["ok"] for event in events)
        join_ok = not invalid_join and contains_any(final_text, ["not signed", "no signed", "cannot", "unsigned"])

    if capability == "multi_step_join":
        # Both signed hops have to be walked. One hop alone is the failure this family exists for.
        executed = [event for event in successful if event["name"] == "benthic_join"]
        expected_paths = expected.get("paths") or []
        join_ok = len(executed) >= len(expected_paths) and all(
            any(_join_matches(event, path) for event in executed) for path in expected_paths
        )

    if capability == "relation_trap":
        # The present-day view returns a confident wrong answer, so the only reliable signal is
        # whether the historical relation was the one actually queried.
        row_count_ok = str(expected.get("relation")) in queried_sources(successful)

    if strict and capability == "discovery" and expected.get("relation"):
        # The legacy scorer only checks that benthic_discover was called, so a playbook that
        # merely nudges one extra tool call would look like progress.
        qualified, bare = discovered_relations(successful)
        row_count_ok = relation_was_discovered(qualified, bare, expected["relation"])

    forbidden_hits = [claim for claim in case.get("forbidden_claims", []) if contains_any(final_text, [claim])]
    return {
        "tool_requirement": tool_requirement,
        "join_check": join_ok,
        "row_count_check": row_count_ok,
        "evidence_check": evidence_ok,
        "answered": answered,
        "forbidden_claim_hits": forbidden_hits,
        "passed": tool_requirement and join_ok and row_count_ok and evidence_ok and answered and not forbidden_hits,
    }


def render_report(results: list[dict[str, Any]], metadata: dict[str, Any], run_id: str, mode: str = "") -> str:
    passed = sum(result["score"]["passed"] for result in results)
    tool_calls = sum(len(result["events"]) for result in results)
    failed_calls = sum(sum(not event["ok"] for event in result["events"]) for result in results)
    elapsed = sum(result["elapsed_ms"] for result in results)
    lines = [
        f"# Evaluation report {run_id}",
        "",
        *([mode] if mode else []),
        "",
        f"Cases: {len(results)}",
        f"Passed: {passed}",
        f"Failed: {len(results) - passed}",
        f"Tool calls: {tool_calls}",
        f"Failed tool calls: {failed_calls}",
        f"Total case time: {elapsed} ms",
        f"Signed join paths: {metadata.get('join_paths')}",
        "",
        "## Cases",
        "",
        "| Case | Capability | Passed | Calls | Failed calls | Time ms |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result['id']} | {result['capability']} | {result['score']['passed']} | {len(result['events'])} | {sum(not event['ok'] for event in result['events'])} | {result['elapsed_ms']} |"
        )
    lines.extend(["", "## Failures", ""])
    for result in results:
        if result["score"]["passed"]:
            continue
        lines.append(f"### {result['id']}")
        lines.append(json.dumps(result["score"], sort_keys=True))
        if result.get("error"):
            lines.append(result["error"])
        lines.append("")
    return "\n".join(lines) + "\n"


async def run_case(
    case: dict[str, Any],
    llm: httpx.AsyncClient,
    mcp: Client,
    model: str,
    tools: list[dict[str, Any]],
    settings: dict[str, Any],
    rep: int = 0,
) -> dict[str, Any]:
    started = time.perf_counter()
    messages: list[dict[str, Any]] = [{"role": "user", "content": case["question"]}]
    events: list[dict[str, Any]] = []
    transcript: list[dict[str, Any]] = []
    final_text = ""
    error = ""
    finish_reason = ""
    usage = Counter()
    try:
        for turn in range(settings["max_turns"]):
            payload = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "temperature": settings["temperature"],
                "top_p": settings["top_p"],
                "top_k": settings["top_k"],
                "max_tokens": settings["max_tokens"],
                "stream": False,
            }
            response = await llm.post(
                f"{settings['llm_url']}/v1/chat/completions", json=payload, timeout=settings["request_timeout"]
            )
            response.raise_for_status()
            data = response.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            transcript.append(
                {"kind": "llm_response", "turn": turn, "message": message, "usage": data.get("usage", {})}
            )
            messages.append(
                {
                    key: value
                    for key, value in message.items()
                    if key in {"role", "content", "reasoning_content", "tool_calls"}
                }
            )
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if data.get("usage", {}).get(key) is not None:
                    usage[key] += data["usage"][key]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                final_text = str(message.get("content") or "")
                finish_reason = str(choice.get("finish_reason") or "")
                break
            for tool_call in tool_calls:
                name = tool_call.get("function", {}).get("name", "")
                raw_arguments = tool_call.get("function", {}).get("arguments", "{}")
                event: dict[str, Any] = {
                    "name": name,
                    "arguments": {},
                    "ok": False,
                    "text": "",
                    "structured": None,
                    "error": "",
                }
                try:
                    arguments = parse_arguments(raw_arguments)
                    event["arguments"] = arguments
                    result = await mcp.call_tool(name, arguments)
                    text, structured, is_error = result_payload(result)
                    event.update({"ok": not is_error, "text": text, "structured": structured})
                    if is_error:
                        event["error"] = text
                    messages.append({"role": "tool", "tool_call_id": tool_call.get("id", ""), "content": text})
                except Exception as exc:
                    event["error"] = str(exc)
                    messages.append(
                        {"role": "tool", "tool_call_id": tool_call.get("id", ""), "content": event["error"]}
                    )
                events.append(event)
                transcript.append({"kind": "tool_call", "turn": turn, "event": event})
        else:
            error = "maximum turns reached"
    except Exception as exc:
        error = str(exc)
    result = {
        "id": case["id"],
        "capability": case["capability"],
        "question": case["question"],
        "expected": case.get("expected", {}),
        "events": events,
        "transcript": transcript,
        "final_text": final_text,
        "rep": rep,
        "score": score_case(case, events, final_text, error, strict=settings.get("strict", False)),
        "usage": dict(usage),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "error": error,
        # "length" means the model ran out of token budget before it could answer, which is a
        # different defect from ending the turn deliberately with nothing, and the two call for
        # different fixes. Without this the only evidence is aggregate token counts.
        "finish_reason": finish_reason,
    }
    return result


def assign_splits(cases: list[dict], holdout_every: int = 4) -> set[str]:
    """Deterministically pick a holdout that spans capabilities.

    A per-case hash randomises which cases land in the holdout but does not guarantee any given
    capability is represented. In practice it drew five easy cases, every arm passed all of them,
    and the tripwire reported no regression while the full suite had lost four cases. A guard that
    cannot fail is worse than no guard, so every capability with at least two cases contributes
    exactly one holdout case, spread evenly through the group.

    The trade-off is that adding cases to a group can move which case is held out, so the holdout
    membership is not stable across suite growth the way a per-case hash is. Stability is the
    lesser concern: a holdout that can actually fail is worth more than one that is comparable.
    """
    groups: dict[str, list[dict]] = {}
    for case in cases:
        groups.setdefault(str(case.get("capability", "")), []).append(case)

    holdout: set[str] = set()
    for capability in sorted(groups):
        group = sorted(groups[capability], key=lambda case: str(case["id"]))
        if len(group) < 2:
            continue
        take = max(1, round(len(group) / holdout_every))
        take = min(take, len(group) - 1)
        for index in range(take):
            holdout.add(str(group[index * len(group) // take]["id"]))
    return holdout


def split_of(case_id: str, holdout_ids: set[str] | None = None) -> str:
    return "holdout" if holdout_ids and case_id in holdout_ids else "tuning"


def select_cases(
    cases: list[dict],
    case_filter: str | None,
    limit: int | None,
    split: str,
    holdout_ids: set[str] | None = None,
) -> list[dict]:
    if case_filter:
        filters = [item.strip() for item in case_filter.split(",") if item.strip()]
        cases = [case for case in cases if any(item in case["id"] for item in filters)]
    if split != "all":
        if holdout_ids is None:
            holdout_ids = assign_splits(cases)
        cases = [case for case in cases if split_of(str(case["id"]), holdout_ids) == split]
    if limit is not None:
        cases = cases[:limit]
    return cases


def in_process_mcp(playbook: str | None):
    """Run the real MCP tool surface in-process against a chosen playbook.

    Used by the A/B gate so a candidate can be measured without restarting the service, and so
    both arms of the comparison see byte-identical tool schemas.
    """
    from benthic_mcp import server as server_module
    from benthic_mcp.config import Settings
    from benthic_mcp.service import BenthicService

    settings = Settings.from_env()
    if playbook in (None, "", "none", "off"):
        settings = replace(settings, playbook_mode="off")
    elif playbook == "seed":
        settings = replace(settings, playbook_mode="seed")
    else:
        settings = replace(settings, playbook_mode="active", playbook_path=Path(playbook))

    service = BenthicService(settings)
    server_module._service = service
    return Client(server_module.mcp, raise_exceptions=True), server_module


async def run_async(args: argparse.Namespace) -> None:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    # Holdout membership is a property of the suite, not of the batch. Deriving it after --case
    # filtering meant a narrow run had no holdout at all, and since --split defaults to "all" the
    # recorded split was missing exactly when the harness needed it.
    holdout_ids = assign_splits(questions["cases"])
    cases = select_cases(questions["cases"], args.case_filter, args.limit, args.split, holdout_ids)
    if not cases:
        raise SystemExit(f"No cases matched split={args.split} filter={args.case_filter}")
    settings = {
        "llm_url": args.llm_url.rstrip("/"),
        "mcp_url": args.mcp_url,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "top_k": args.top_k,
        "max_tokens": args.max_tokens,
        "max_turns": args.max_turns,
        "request_timeout": args.request_timeout,
        "strict": args.strict,
    }

    token = None if args.in_process else read_token()
    if args.in_process:
        stack = contextlib.AsyncExitStack()
        mcp = await stack.enter_async_context(in_process_mcp(args.playbook)[0])
    else:
        stack = contextlib.AsyncExitStack()
        mcp = await stack.enter_async_context(Client(AuthTransport(args.mcp_url, token), raise_exceptions=True))

    async with stack, httpx.AsyncClient(timeout=args.request_timeout) as llm:
        model = await model_id(llm, settings["llm_url"], args.model)
        tools = tool_definitions((await mcp.list_tools()).tools)
        results = []
        for index, case in enumerate(cases, 1):
            for rep in range(args.reps):
                suffix = f" rep {rep + 1}/{args.reps}" if args.reps > 1 else ""
                print(f"[{index}/{len(cases)}] {case['id']}{suffix}", flush=True)
                results.append(await run_case(case, llm, mcp, model, tools, settings, rep=rep))

    mode = f"strict scoring | {args.reps} rep(s) | split={args.split} | playbook={args.playbook or 'http'}"
    report = render_report(results, questions["metadata"], run_id, mode)
    (output_dir / "questions.json").write_text(
        json.dumps({**questions, "cases": cases}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "results.json").write_text(
        json.dumps(results, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    with (output_dir / "transcript.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, sort_keys=True, default=str) + "\n")
    (output_dir / "failures.json").write_text(
        json.dumps(
            [result for result in results if not result["score"]["passed"]], indent=2, sort_keys=True, default=str
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    # Self-describing arm metadata: the harness compares runs and must never mix modes.
    (output_dir / "run_meta.json").write_text(
        json.dumps(
            {
                "cases": len(cases),
                "reps": args.reps,
                "split": args.split,
                "holdout_ids": sorted(holdout_ids),
                "strict": args.strict,
                "in_process": args.in_process,
                "playbook": args.playbook or ("http" if not args.in_process else "seed"),
                "max_turns": args.max_turns,
                "temperature": args.temperature,
                "model": model,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(report)
    print(f"Artifacts: {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", default=str(ROOT / "eval" / "generated" / "questions.json"))
    parser.add_argument("--output-dir", default=str(ROOT / "eval" / "runs"))
    parser.add_argument("--llm-url", default="http://192.168.10.222:8081")
    parser.add_argument("--mcp-url", default="http://192.168.10.222:8082/mcp")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-filter")
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument("--split", choices=["all", "tuning", "holdout"], default="all")
    parser.add_argument("--in-process", action="store_true", help="run the MCP server in-process")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="require a real final answer and check the expected relation was actually retrieved",
    )
    parser.add_argument("--reps", type=int, default=1, help="repeat each case N times to expose per-case noise")
    parser.add_argument("--playbook", help="playbook path, or 'seed' / 'none'; requires --in-process")
    args = parser.parse_args()
    if args.playbook and not args.in_process:
        parser.error("--playbook requires --in-process")
    asyncio.run(run_async(args))


if __name__ == "__main__":
    main()
