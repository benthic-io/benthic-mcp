"""Measure the NGOpen combination suite against the server as it is, and write nothing.

The suite arrives as 100 English questions tagged `now`, `grow` or `refuse`, with no
expected values. That is deliberate and correct: an `expected` written by whoever ran
the tool last would describe whatever the server happened to do, which is how a suite
becomes a rubber stamp. This script therefore does not score. It drives every question,
keeps the whole transcript including reasoning, and leaves the judgement to a reader.

What it emits is the shape of the work, not a verdict on it:

  - per case: answered or not, which tools were called, whether the answer named a
    relation, and what the server said when it refused
  - per case: the *missing hop*, when the model's own reasoning names one. That is the
    most useful signal in the whole run and it costs nothing to extract, because a model
    that says "there is no signed path from X to Y" is telling us exactly what category 3
    of the brief would require.
  - refusals are separated from failures, because the brief requires all seven `refuse`
    items to keep refusing and a refusal is the correct outcome for those.

Refusals are read from the server's own error text rather than from the model's prose.
That is the same rule the observer uses, and for the same reason: prose matching finds
filler. "Let me try" is not a refusal.

Usage:
    .venv/bin/python eval/combination/classify.py --out eval/combination/run-<stamp>
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

BASE = "http://192.168.10.222:8081"
SUITE = Path(__file__).resolve().parents[2] / "grok" / "ngopen-combination-suite.json"

# Relations the model named that the catalog does not carry. Cheap, and it is the same
# check the observer's findings make, so a hallucinated relation is visible here too.
CATALOG_RELATIONS: set[str] = set()
# Column names, qualified by dataset where the manifest allows it. A model writing
# `irs_ng.ein` is naming a column, not inventing a relation, and a check that reports it
# as a missing relation would send someone to "fix" a relation that does not need fixing.
CATALOG_COLUMNS: set[str] = set()


def load_catalog_relations() -> set[str]:
    """Read the live signed catalog so a named relation can be checked against it."""
    for candidate in (
        Path.home() / ".cache/benthic-mcp/verified-catalog.json",
        Path.home() / ".cache/benthic-mcp/ngopen-catalog.json",
    ):
        if not candidate.is_file():
            continue
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        found: set[str] = set()
        columns: set[str] = set()

        def walk(node: Any, dataset: str | None = None) -> None:
            if isinstance(node, dict):
                dataset = node.get("dataset_name") or dataset
                name = node.get("name")
                if isinstance(name, str) and "relation_type" in node and "columns" in node:
                    found.add(name)
                    for column in node.get("columns") or []:
                        if isinstance(column, dict) and isinstance(column.get("name"), str):
                            columns.add(f"{dataset}.{column['name']}" if dataset else column["name"])
                for value in node.values():
                    walk(value, dataset)
            elif isinstance(node, list):
                for value in node:
                    walk(value, dataset)

        walk(document)
        if found:
            global CATALOG_COLUMNS
            CATALOG_COLUMNS = columns
            return found
    return set()


def post(path: str, payload: dict[str, Any] | None, timeout: float) -> Any:
    """POST JSON to llama-server. A payload of None sends no body at all.

    `/tools` is a POST with an empty body; sending `{}` there returns 400, which is not a
    server fault and would otherwise read as one.
    """
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def tools() -> list[dict[str, Any]]:
    """List the MCP tools. Listing is a GET; POST /tools executes one and 400s otherwise.

    Getting this wrong looks like the server being down, which is the kind of misreading
    this instrument exists to prevent.
    """
    request = urllib.request.Request(f"{BASE}/tools", method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        listing = json.loads(response.read().decode("utf-8"))
    return [tool["definition"] for tool in listing]


# Transport-level failures. These are about the connection, not about the case, and must
# never be recorded as a refusal, a failure to answer, or a missing hop.
CONNECTION_ERRORS = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    ConnectionResetError,
    ConnectionAbortedError,
    TimeoutError,
    json.JSONDecodeError,
)

# A refusal is the server declining, in its own words. Prose matching finds filler.
_REFUSAL_MARKERS = (
    "is not a signed BDP join path",
    "declares no primary key",
    "past the complete-scan limit",
    "rows match the filters",
    "must use dataset.relation",
    "is not a queryable relation",
    "not in the signed catalog",
    "no signed join",
)

# A phrase that names the hop a case needs and the server lacks. This is the signal that
# turns a failure into a category, without anyone deciding which category by feel.
_MISSING_HOP = re.compile(
    r"no signed (?:join|path|edge)[^.]*?"
    r"(?:from|between)\s+([A-Za-z_][\w.]*)\s+(?:to|and)\s+([A-Za-z_][\w.]*)",
    re.IGNORECASE,
)
# The model puts the two relations on either side of the statement of absence, and it
# does not put them in a fixed order: "the join from A to B is not signed", but also
# "A and B are not connected". Both phrasings occur, so both are matched. Anchoring on
# the absence marker alone is what makes this reliable: a sentence that merely mentions
# two relations is not a claim that a hop is missing.
_MISSING_HOP_LOOSE = re.compile(
    r"(?:from\s+(?P<a>[A-Za-z_][\w.]*)\s+to\s+(?P<b>[A-Za-z_][\w.]*)[^.]*?"
    r"(?:is|are)\s+not\s+\w+)"
    r"|(?:(?P<c>[A-Za-z_][\w.]*)\s+and\s+(?P<d>[A-Za-z_][\w.]*)[^.]*?"
    r"(?:is|are)\s+not\s+(?:connected|joined|linked|signed))",
    re.IGNORECASE,
)


def classify_text(text: str) -> tuple[bool, str | None]:
    """Return (server_refused, missing_hop) for one blob of text."""
    refused = any(marker in text for marker in _REFUSAL_MARKERS)
    hop: str | None = None
    match = _MISSING_HOP.search(text)
    if match:
        hop = f"{match.group(1)} -> {match.group(2)}"
    else:
        match = _MISSING_HOP_LOOSE.search(text)
        if match:
            left = match.group("a") or match.group("c")
            right = match.group("b") or match.group("d")
            if left and right:
                hop = f"{left} -> {right}"
    return refused, _plausible_hop(hop)


def _plausible_hop(hop: str | None) -> str | None:
    """Keep a named hop only when both ends are relations the catalog actually carries.

    The regexes match sentence shape, not meaning, so they fire on ordinary prose: a
    58-case run produced `prime_awards -> the` and `awards -> foundations`, neither of
    which is a hop. A finding that survives this check is two real relations the model
    says are not connected, which is the thing category 3 is about.
    """
    if not hop or not CATALOG_RELATIONS:
        return hop
    bare = {name.rsplit(".", 1)[-1] for name in CATALOG_RELATIONS}
    left, _, right = hop.partition("->")
    if left.strip().rstrip(".").rsplit(".", 1)[-1] in bare and right.strip().rstrip(".").rsplit(".", 1)[-1] in bare:
        return hop
    return None


def run_case(
    question: str, tool_list: list[dict[str, Any]], max_turns: int, timeout: float, max_tokens: int
) -> dict[str, Any]:
    """Drive one question the way the web UI does: the model emits tool_calls and stops."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    turns: list[dict[str, Any]] = []
    called: list[str] = []
    server_text: list[str] = []
    call_timings: list[dict[str, Any]] = []
    answered = False
    length_cut = False
    broken = 0

    for number in range(1, max_turns + 1):
        try:
            response = post(
                "/v1/chat/completions",
                {
                    "model": "local",
                    "messages": messages,
                    "tools": tool_list,
                    # The model reasons before it answers, and a turn that exhausts this
                    # budget mid tool-call is indistinguishable from a refusal. Measured:
                    # one probe spent 4,000 tokens on deliberation and was cut inside a
                    # tool call that never reached the server. Then at 8,000: every hard case
                    # ended on a reasoning block of ~32,500 characters, which is 8,000 tokens,
                    # so the model was cut mid-thought and never emitted a tool call at all.
                    "max_tokens": max_tokens,
                    "temperature": 0.0,
                    "top_p": 0.95,
                },
                timeout,
            )
        except CONNECTION_ERRORS as exc:
            # A dropped connection is a property of the transport, not of the case. One
            # RemoteDisconnected killed a 58-case run outright, which threw away an hour of
            # model time and left the refusal floor unmeasured. The case records the error
            # and the run continues; the summary reports how many cases ended this way so a
            # transport failure is never silently counted as a model failure.
            transport_error = f"{type(exc).__name__}: {exc}"
            turns.append({"turn": number, "error": transport_error})
            break

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        reasoning = message.get("reasoning_content") or ""
        content = message.get("content") or ""
        finish = choice.get("finish_reason")

        if finish == "length":
            length_cut = True
        if reasoning.strip():
            turns.append({"turn": number, "reasoning": reasoning})
        if content.strip():
            turns.append({"turn": number, "content": content})
            answered = True

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            break

        messages.append(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"]},
                    }
                    for call in tool_calls
                ],
            }
        )
        for call in tool_calls:
            name = call["function"]["name"]
            called.append(name)
            try:
                arguments = json.loads(call["function"]["arguments"])
            except json.JSONDecodeError:
                broken += 1
                call_timings.append({"tool": name, "seconds": 0.0, "error": True})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": "malformed arguments"})
                continue
            started = time.monotonic()
            try:
                result = post("/tools", {"tool": name, "params": arguments}, timeout)
                text = result.get("plain_text_response") or json.dumps(result)
                call_timings.append({"tool": name, "seconds": round(time.monotonic() - started, 2), "error": False})
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                text = f"{type(exc).__name__}: {exc}"
                call_timings.append({"tool": name, "seconds": round(time.monotonic() - started, 2), "error": True})
                broken += 1
            server_text.append(text)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})

    refused = False
    hop: str | None = None
    for text in server_text:
        got_refused, got_hop = classify_text(text)
        refused = refused or got_refused
        hop = hop or got_hop

    # The model's own reasoning is where it names what is missing, and it does so before
    # it gives up. Reasoning is included in the scan; server_text is what decides refusal.
    if not hop:
        for turn in turns:
            got_refused, got_hop = classify_text(turn.get("reasoning") or "")
            hop = hop or got_hop

    return {
        "answered": answered,
        "server_refused": refused,
        "missing_hop": hop,
        "tools_called": called,
        "distinct_tools": sorted(set(called)),
        "turns": turns,
        "server_text": server_text,
        # Per-call, so a slow case can be split into slow server and slow model. A run of
        # 100 cases where 43 exceeded 295s could not be attributed either way before this.
        "tool_timings": call_timings,
        "length_cut": length_cut,
        "broken_calls": broken,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="directory to write the run into")
    parser.add_argument("--suite", default=str(SUITE))
    parser.add_argument("--only", help="comma-separated ids or tags to run")
    parser.add_argument("--tags", help="comma-separated tags to restrict to")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="keep the records already in --out and run only the cases missing from them",
    )
    args = parser.parse_args()

    global CATALOG_RELATIONS
    CATALOG_RELATIONS = load_catalog_relations()
    if not CATALOG_RELATIONS:
        print("WARNING: no catalog found, relation checks will be skipped", flush=True)

    cases = json.loads(Path(args.suite).read_text(encoding="utf-8"))
    if isinstance(cases, dict):
        cases = cases.get("cases") or cases.get("items") or []
    if args.only:
        wanted = {item.strip() for item in args.only.split(",")}
        cases = [c for c in cases if c.get("id") in wanted]
    if args.tags:
        tags = {item.strip() for item in args.tags.split(",")}
        cases = [c for c in cases if c.get("tag") in tags]

    # The floor goes first. The suite file interleaves tags and puts all seven `refuse`
    # cases at positions 89-95, so a run that stops early - which is what happened, at 58 of
    # 100 - measures the floor not at all. The seven cases that must keep refusing are the
    # cheapest signal in the suite and the one that says whether anything broke.
    priority = {"refuse": 0, "now": 1, "grow": 2}
    cases = sorted(cases, key=lambda c: (priority.get(c.get("tag"), 3),))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    records = out_dir / "cases.jsonl"

    # Resume rather than restart. A dropped connection killed a run at 58 of 100 after an
    # hour of model time, and the cases that completed were already on disk. Re-running them
    # would spend the same hour again and produce slightly different numbers, which is worse
    # than useless: it would make the two halves of one suite incomparable.
    prior: list[dict[str, Any]] = []
    if args.resume and records.is_file():
        for line in records.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    prior.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        done = {row.get("id") for row in prior}
        before = len(cases)
        cases = [c for c in cases if c.get("id") not in done]
        print(
            f"resuming: {len(prior)} already recorded, {len(cases)} of {before} remaining",
            flush=True,
        )

    tool_list = tools()

    rows: list[dict[str, Any]] = list(prior)
    total = len(cases)
    for index, case in enumerate(cases, 1):
        started = time.monotonic()
        result = run_case(case["question"], tool_list, args.max_turns, args.timeout, args.max_tokens)
        row = {
            "id": case.get("id"),
            "tag": case.get("tag"),
            "domain": case.get("domain"),
            "question": case["question"],
            "elapsed_s": round(time.monotonic() - started, 1),
            **result,
        }
        rows.append(row)
        flag = "R" if result["server_refused"] else ("A" if result["answered"] else "-")
        print(f"[{index}/{total}] {row['id']:24} {flag} {row['elapsed_s']:>6}s", flush=True)
        (out_dir / "cases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    summary = summarise(rows)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print()
    for line in render(summary):
        print(line)
    return 0


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_tag: dict[str, Counter] = {}
    for row in rows:
        tag = row.get("tag") or "?"
        bucket = by_tag.setdefault(tag, Counter())
        bucket["n"] += 1
        bucket["answered"] += 1 if row["answered"] else 0
        bucket["refused"] += 1 if row["server_refused"] else 0
        bucket["length_cut"] += 1 if row["length_cut"] else 0
        bucket["broken_calls"] += 1 if row["broken_calls"] else 0
        bucket["named_a_hop"] += 1 if row.get("missing_hop") else 0

    hops: Counter = Counter()
    for row in rows:
        if row.get("missing_hop"):
            hops[row["missing_hop"]] += 1

    tools_used: Counter = Counter()
    for row in rows:
        tools_used.update(row.get("distinct_tools") or [])

    # Split every case into time the server spent and time the model spent. Without this a slow case
    # is ambiguous: 43 of 100 exceeded 295s and nothing recorded said whether the query was slow or
    # the deliberation was. Per-tool below, per-run here.
    tool_seconds = 0.0
    elapsed_seconds = 0.0
    slowest_tool: tuple[float, str] | None = None
    for row in rows:
        elapsed_seconds += float(row.get("elapsed_s") or 0.0)
        for timing in row.get("tool_timings") or []:
            seconds = float(timing.get("seconds") or 0.0)
            tool_seconds += seconds
            if slowest_tool is None or seconds > slowest_tool[0]:
                slowest_tool = (seconds, str(timing.get("tool")))

    # Every relation the model named, checked against the signed catalog. A relation the
    # server does not carry is a hallucination regardless of how well the answer reads.
    #
    # The catalog keys relations by bare name and the model writes them qualified, so the
    # comparison is against the part after the dot. Checking the qualified string against a
    # bare-keyed set reports every real relation as absent, which is how a check that looks
    # like it is working produces the opposite result.
    bare_relations = {name.rsplit(".", 1)[-1] for name in CATALOG_RELATIONS}
    named: Counter = Counter()
    for row in rows:
        if not CATALOG_RELATIONS:
            break
        for turn in row.get("turns") or []:
            for blob in (turn.get("reasoning") or "", turn.get("content") or ""):
                for match in re.finditer(r"\b([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\b", blob):
                    candidate = match.group(1)
                    if candidate.split(".")[0] in {"usaspending", "samer", "irs_ng", "usp_cl", "up_cdmaps"}:
                        named[candidate] += 1
    # A qualified name the model used is absent only if it is neither a relation nor a
    # column. `irs_ng.ein` is a column of bmf_organizations and `usaspending.geom_point`
    # is a column of all_entities; both were reported as missing relations, which reads as
    # a catalog defect when it is a check that does not know the difference.
    bare_columns = {name.rsplit(".", 1)[-1] for name in CATALOG_COLUMNS}
    unknown = {
        name: count
        for name, count in named.items()
        if name.rsplit(".", 1)[-1] not in bare_relations and name.rsplit(".", 1)[-1] not in bare_columns
    }

    return {
        "cases": len(rows),
        "by_tag": {tag: dict(counts) for tag, counts in sorted(by_tag.items())},
        "time": {
            "case_seconds": round(elapsed_seconds, 1),
            "tool_seconds": round(tool_seconds, 1),
            "model_seconds": round(elapsed_seconds - tool_seconds, 1),
            "tool_share": round(tool_seconds / elapsed_seconds, 3) if elapsed_seconds else None,
            "slowest_call": ({"tool": slowest_tool[1], "seconds": slowest_tool[0]} if slowest_tool else None),
        },
        "missing_hops": hops.most_common(),
        "tools_used": tools_used.most_common(),
        "relations_named_but_absent": sorted(unknown.items(), key=lambda kv: -kv[1]),
        "never_answered": [r["id"] for r in rows if not r["answered"]],
        "length_cut": [r["id"] for r in rows if r["length_cut"]],
    }


def render(summary: dict[str, Any]) -> list[str]:
    lines = ["", "=== by tag ==="]
    for tag, counts in summary["by_tag"].items():
        lines.append(
            f"  {tag:8} n={counts['n']:<4} answered={counts['answered']:<4} "
            f"refused={counts['refused']:<4} named_a_hop={counts['named_a_hop']:<4} "
            f"length_cut={counts['length_cut']}"
        )
    lines += ["", "=== missing hops named by the model ==="]
    if summary["missing_hops"]:
        for hop, count in summary["missing_hops"]:
            lines.append(f"  x{count:<4} {hop}")
    else:
        lines.append("  none")
    lines += ["", "=== tools used ==="]
    for name, count in summary["tools_used"]:
        lines.append(f"  x{count:<4} {name}")
    if summary["relations_named_but_absent"]:
        lines += ["", "=== relations named that the catalog does not carry ==="]
        for name, count in summary["relations_named_but_absent"][:12]:
            lines.append(f"  x{count:<4} {name}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
