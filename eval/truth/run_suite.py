"""Drive the graded suite through the model and grade the transcripts.

Separated from `eval/truth/grade.py` so grading stays deterministic and free: this file spends model
time, that one does not. Re-grading a stored run costs nothing and never changes its verdict, which is
what makes it safe to re-check an old run against a corrected grader.

Deliberately not `eval/combination/classify.py`. That one drives the ungraded 100-question suite and
does not score, which is right for it: an `expected` written by whoever ran the tool last would
describe whatever the server happened to do. This suite's expected values come from the database at
generation time, so they can be graded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval" / "truth"))

import grade as grader  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = ROOT / "eval" / "generated" / "questions.json"
MODEL_URL = "http://127.0.0.1:8081"


async def post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


async def call_tool(name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
    raw = await post(f"{MODEL_URL}/tools", {"tool": name, "params": arguments}, timeout)
    text = raw.get("plain_text_response")
    if text is None:
        return {"ok": False, "text": json.dumps(raw)}
    return {"ok": True, "text": text}


async def manifest_names() -> tuple[set[str], set[str]]:
    """Every relation and every column the manifest carries.

    Both are needed by the anti-hallucination floor: relations to catch an invented table, and columns
    so that a column alias is not mistaken for one.
    """
    """Every relation the manifest carries, for the anti-hallucination floor."""
    sys.path.insert(0, str(ROOT / "src"))
    from benthic_mcp.bdp import BdpRepository  # type: ignore[import-not-found]
    from benthic_mcp.catalog import Catalog  # type: ignore[import-not-found]
    from benthic_mcp.config import Settings  # type: ignore[import-not-found]

    settings = Settings.from_env()
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        repository = BdpRepository(settings, client)
        snapshot = await repository.load()
    catalog = Catalog(snapshot)
    relations = {f"{dataset}.{relation}" for dataset, relation in catalog.relations}
    columns = {column for definition in catalog.relations.values() for column in definition.columns}
    return relations, columns


async def drive(
    case: dict[str, Any], max_turns: int, timeout: float, temperature: float, max_tokens: int
) -> dict[str, Any]:
    """One case, one conversation, transcript kept whole.

    Reasoning is recorded because the grader reads it: a model can assert something in prose that it
    never verified, and a grader that cannot see that is grading the last sentence only.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": case["question"]}]
    turns: list[dict[str, Any]] = []
    started = time.time()
    answer = ""

    for turn_number in range(1, max_turns + 1):
        response = await post(
            f"{MODEL_URL}/v1/chat/completions",
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": await tool_schemas(),
            },
            timeout,
        )
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        # This server runs with --reasoning-preserve, so a thinking turn puts its output in
        # `reasoning_content` and leaves `content` empty. Reading only `content` records a blank
        # transcript, and the grader reads the reasoning by design - a model asserting something in its
        # reasoning is exactly what it must be able to catch. sweep.py documents the same trap.
        content = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or "")
        said = "\n".join(part for part in (reasoning, content) if part)
        calls = message.get("tool_calls") or []
        record: dict[str, Any] = {
            "turn": turn_number,
            "content": content,
            "reasoning": reasoning,
            "finish_reason": choice.get("finish_reason"),
            "tool_results": [],
        }

        if not calls:
            answer = said
            turns.append(record)
            break

        messages.append({"role": "assistant", "content": said or None, "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            result = await call_tool(name, arguments, timeout)
            record["tool_results"].append({"name": name, "args": arguments, **result})
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id", ""), "content": str(result.get("text", ""))[:6000]}
            )
        turns.append(record)

    return {
        "id": case.get("id"),
        "capability": case.get("capability"),
        "question": case.get("question"),
        "answer": answer,
        "turns": turns,
        "elapsed_s": round(time.time() - started, 1),
        "answered": bool(answer),
    }


async def tool_schemas() -> list[dict[str, Any]]:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(f"{MODEL_URL}/tools")
            response.raise_for_status()
            document = response.json()
        except Exception:
            return []
    # /tools serves [{"name":..., "definition": {"type": "function", "function": {...}}}], so the
    # definition is already the chat-API shape and passes through unchanged. Reading `name` off the
    # definition instead of off `function` yields an empty list, and the model then answers from prose
    # with no access to the server - which grades as a total failure that means nothing.
    return [
        tool["definition"]
        for tool in (document if isinstance(document, list) else [])
        if isinstance(tool, dict) and isinstance(tool.get("definition"), dict)
    ]


async def run(args: argparse.Namespace) -> int:
    cases = grader.load_cases(str(args.questions))
    if args.capability:
        wanted = set(args.capability.split(","))
        cases = [c for c in cases if str(c.get("capability")) in wanted]
    if args.limit:
        cases = cases[: args.limit]

    relations: set[str] | None = None
    columns: set[str] | None = None
    if not args.no_manifest_floor:
        try:
            relations, columns = await manifest_names()
        except Exception as exc:  # the floor is valuable but the run should still produce numbers
            print(f"  anti-hallucination floor unavailable: {exc}", file=sys.stderr)

    runs: list[dict[str, Any]] = []
    started = time.time()
    for index, case in enumerate(cases, 1):
        print(f"  [{index}/{len(cases)}] {case.get('id')} ({case.get('capability')})", flush=True)
        try:
            runs.append(await drive(case, args.max_turns, args.request_timeout, args.temperature, args.max_tokens))
        except Exception as exc:
            runs.append(
                {
                    "id": case.get("id"),
                    "capability": case.get("capability"),
                    "question": case.get("question"),
                    "answer": "",
                    "turns": [],
                    "answered": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    by_id = {str(c.get("id")): c for c in cases}
    results = [grader.grade_case(by_id[str(run_["id"])], run_, relations) for run_ in runs if str(run_["id"]) in by_id]
    summary = grader.summarise(results)

    args.output.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    run_path = args.output / f"run-{stamp}.json"
    run_path.write_text(json.dumps({"runs": runs, "results": [r.as_dict() for r in results]}, indent=2))

    print()
    print(f"  suite: {len(cases)} cases in {time.time() - started:.0f}s -> {run_path.name}")
    print()
    print("  per capability (no overall rate: RPCs are exact, joins are partly heuristic)")
    print(f"  {'capability':<36}{'pass':>6}{'rate':>8}  worst failing check")
    for capability, bucket in sorted(summary["by_capability"].items()):
        worst = next(iter(bucket["failed_checks"]), "")
        rate = "n/a" if bucket["rate"] is None else f"{bucket['rate']:.0%}"
        print(f"  {capability:<36}{bucket['passed']:>3}/{bucket['cases']:<3}{rate:>8}  {worst}")
    print()
    unanswered = [r for r in results if not r.tools_called]
    if unanswered:
        print(f"  {len(unanswered)} case(s) called no tool at all - those grade on prose alone")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output", type=Path, default=ROOT / "eval" / "truth" / "runs")
    parser.add_argument("--capability", help="comma-separated capability names")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-turns", type=int, default=8)
    # 2048 truncated a reasoning turn on relation_trap: 8,590 characters of reasoning and
    # finish_reason: length. sweep.py raised this to 8,000 for the same reason, after one turn spent its
    # whole budget on reasoning. A truncated turn records as a failure that is not the model's.
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument("--no-manifest-floor", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
