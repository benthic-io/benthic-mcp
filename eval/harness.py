"""Usage-driven improvement round: run the questions, learn from the failures, accumulate.

One round is:

1. Run a usage batch (the generated questions) against the currently accumulated playbook.
2. Turn each result into an objective trace and detect struggles.
3. Ask the calling model, via a reflection turn, what a future session should do differently.
4. Write the lesson through `BenthicService.record_lesson`, the same path as `benthic_report`.
5. Consolidate cumulatively, enforcing the lesson cap.

Everything happens inside `BENTHIC_CACHE_DIR`, so the traces, lessons, and playbook belong to
this harness alone. The live service is never touched; `promote.py --harness-run` is the only
way an accumulated playbook reaches it.

The reflection prompt is built without the case oracle. `reflect.assert_no_oracle` enforces it.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflect import oracle_only_values, reflect  # noqa: E402

from benthic_mcp.catalog import Catalog  # noqa: E402
from benthic_mcp.config import Settings  # noqa: E402
from benthic_mcp.playbook import Playbook  # noqa: E402
from benthic_mcp.service import BenthicService  # noqa: E402
from benthic_mcp.trace import (  # noqa: E402
    LessonStore,
    StruggleSignature,
    TraceEntry,
    struggle_signatures,
)


@dataclass(slots=True)
class CaseUsage:
    case_id: str
    capability: str
    passed: bool
    error: str
    events: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    question: str = ""
    tools: list[str] = field(default_factory=list)
    signatures: list[dict[str, Any]] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)
    seen: str = ""


_CHECK_EXPLANATIONS = {
    "answered": "the agent never produced a final answer, so nothing was delivered",
    "tool_requirement": "the agent never made the required tool call",
    "join_check": "the agent did not run the required query sequence or join",
    "row_count_check": "the agent never retrieved the source it needed",
    "evidence_check": "the answer misstated how reliable the evidence was",
}


def failed_check_signature(record: dict[str, Any]) -> dict[str, Any] | None:
    """The usage signal the server cannot see: strict scoring rejected the run.

    Only check names are surfaced. The expected relation, values, and row counts stay in the oracle,
    so this tells the reflector that the method failed without revealing what the right answer was.
    """
    score = record.get("score") or {}
    failed = [name for name, passed in score.items() if name != "passed" and passed is False]
    forbidden = score.get("forbidden_claim_hits") or []
    if not failed and not forbidden:
        return None
    reasons = [_CHECK_EXPLANATIONS[name] for name in failed if name in _CHECK_EXPLANATIONS]
    reasons.extend(f"the answer asserted something it should not have: {claim}" for claim in forbidden)
    required = record.get("required_tools") or []
    if "tool_requirement" in failed and required:
        reasons.append(f"the required tools were: {', '.join(required)}")
    return {
        "kind": "failed_check",
        "tool": "scoring",
        "detail": "; ".join(reasons),
        "source": None,
        "severity": 3,
    }


def usage_from_result(record: dict[str, Any]) -> CaseUsage:
    events = record.get("events", [])
    entries = [
        TraceEntry(
            tool=event.get("name", "unknown"),
            ok=bool(event.get("ok")),
            error=str(event.get("error"))[:200] or None,
            row_count=(event.get("structured") or {}).get("row_count"),
            truncated=(event.get("structured") or {}).get("truncated"),
            source_complete=(event.get("structured") or {}).get("source_complete"),
            sources=[
                source.get("source")
                for source in (event.get("structured") or {}).get("sources", [])
                if isinstance(source, dict) and source.get("source")
            ]
            or _argument_sources(event.get("arguments") or {}),
        )
        for event in events
    ]
    signatures = struggle_signatures(entries, max_turns_hit=record.get("error") == "maximum turns reached")
    if record.get("finish_reason") == "length":
        # The model spent its whole token budget without producing an answer. Distinct from running
        # out of turns: more turns would not help, and neither would telling it to answer sooner.
        signatures.append(
            StruggleSignature(
                kind="token_exhausted",
                tool="llm",
                detail="the model used its entire token budget and returned no content",
                source=None,
                severity=4,
            )
        )
    scored = failed_check_signature(record)
    if scored is not None:
        signatures.append(StruggleSignature(**scored))
    return CaseUsage(
        case_id=record["id"],
        capability=record.get("capability", ""),
        passed=bool(record["score"]["passed"]),
        error=str(record.get("error", "")),
        events=events,
        final_text=record.get("final_text", ""),
        question=record.get("question", ""),
        tools=[event.get("name", "?") for event in events],
        signatures=[signature.to_json() for signature in signatures],
        expected=record.get("expected", {}) or {},
        seen=json.dumps(events, default=str) + str(record.get("final_text", "")),
    )


def _argument_sources(arguments: dict[str, Any]) -> list[str]:
    names = ("source", "left_source", "right_source", "operation", "relation", "dataset")
    return [str(arguments[name]) for name in names if arguments.get(name)]


def run_usage_batch(args: argparse.Namespace, sandbox: Path, playbook: str) -> tuple[Path, list[dict[str, Any]]]:
    """Shell out to the eval runner so the usage batch is byte-identical to a normal eval run."""
    if args.reuse_run:
        run_dir = Path(args.reuse_run)
        records = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
        print(f"Reusing usage batch: {run_dir}", flush=True)
        return run_dir, records
    command = [
        sys.executable,
        str(ROOT / "eval" / "run_eval.py"),
        "--in-process",
        "--strict",
        "--split",
        args.split,
        "--reps",
        str(args.reps),
        "--llm-url",
        args.llm_url,
        "--max-turns",
        str(args.max_turns),
        "--output-dir",
        str(sandbox / "runs"),
        "--playbook",
        playbook,
    ]
    if args.no_thinking:
        command += ["--no-thinking"]
    if args.model:
        command += ["--model", args.model]
    if args.case_filter:
        command += ["--case-filter", args.case_filter]
    # Streamed rather than captured: the per-case progress is the thing you want to watch, and the
    # results are read from the run directory rather than from stdout.
    completed = subprocess.run(
        command, cwd=str(ROOT), stdout=None, stderr=subprocess.STDOUT, env={**os.environ, **args.env}
    )
    if completed.returncode != 0:
        raise SystemExit(f"usage batch failed with exit code {completed.returncode}")
    run_dir = sorted((sandbox / "runs").iterdir())[-1]
    records = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    return run_dir, records


def select_for_reflection(
    usages: list[CaseUsage], limit: int, holdout_ids: frozenset[str] = frozenset()
) -> list[CaseUsage]:
    """Reflect on anything that went wrong, on tuning cases only.

    A clean tool trace with a wrong answer produces no server-side signal at all, so the strict
    failure is the only remaining usage signal for that class of mistake.

    Holdout cases are excluded. A lesson extracted from a case and then scored on that same case
    makes the score a training number, which is why the round-to-round trend could read flat while
    saying nothing about whether accumulation helped. The guard is the only reader of the holdout.
    """
    struggling = [
        usage for usage in usages if (usage.signatures or not usage.passed) and usage.case_id not in holdout_ids
    ]

    def severity(usage: CaseUsage) -> int:
        return max((sig["severity"] for sig in usage.signatures), default=2)

    struggling.sort(key=lambda usage: (-severity(usage), usage.case_id))
    return struggling[:limit]


def rediscovery(records: list[dict[str, Any]]) -> dict[str, Any]:
    """How much work a session spent re-deriving things it could have looked up.

    Pass rate cannot see this. A 33-case suite read flat straight through a real regression, and the
    failure that motivated the relation-pair path lookup was a model calling benthic_discover five
    or six times and never calling benthic_query at all, which is a 0/1 on a case either way.

    The number to watch is discovery calls made before the first query: a session that has found its
    relation and is still discovering is re-deriving. Repeated (tool, source) pairs are the same
    symptom when a session re-reads the same schema with slightly different arguments.
    """
    per_case: dict[str, Any] = {}
    for record in records:
        events = record.get("events", [])
        names = [event.get("name", "") for event in events]
        before_query = len(names)
        for index, name in enumerate(names):
            if name in {"benthic_query", "benthic_join", "benthic_rpc"}:
                before_query = index
                break
        targets = [
            (
                event.get("name", ""),
                (event.get("arguments") or {}).get("source") or (event.get("arguments") or {}).get("left_source") or "",
            )
            for event in events
        ]
        repeated = sum(count - 1 for count in Counter(targets).values() if count > 1)
        per_case[record["id"]] = {
            "calls": len(events),
            "discovers": names.count("benthic_discover"),
            "discovers_before_first_use": names[:before_query].count("benthic_discover"),
            "repeated_targets": repeated,
            "answered": bool(record.get("final_text", "").strip()),
        }
    answered = [entry for entry in per_case.values() if entry["answered"]]
    return {
        "per_case": per_case,
        "mean_calls": round(sum(e["calls"] for e in per_case.values()) / max(1, len(per_case)), 2),
        "mean_discovers_before_first_use": round(
            sum(e["discovers_before_first_use"] for e in per_case.values()) / max(1, len(per_case)), 2
        ),
        "mean_repeated_targets": round(
            sum(e["repeated_targets"] for e in per_case.values()) / max(1, len(per_case)), 2
        ),
        "mean_calls_when_answered": (round(sum(e["calls"] for e in answered) / len(answered), 2) if answered else None),
    }


def rep_flip_rate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-case agreement across repetitions.

    No other number in this project can be interpreted without it. A case passing 4/5 looks like a
    clean pass in a single-rep trend, and two single-rep flips on two holdout cases were the entire
    difference between a detected regression and none.
    """
    outcomes: dict[str, list[bool]] = {}
    for record in records:
        outcomes.setdefault(str(record["id"]), []).append(bool(record["score"]["passed"]))

    per_case = {
        case_id: {"passes": sum(results), "reps": len(results), "flaky": 0 < sum(results) < len(results)}
        for case_id, results in sorted(outcomes.items())
    }
    repeated = [entry for entry in per_case.values() if entry["reps"] > 1]
    flaky = sorted(case_id for case_id, entry in per_case.items() if entry["flaky"])
    return {
        "per_case": per_case,
        "flaky_cases": flaky,
        "flaky_share": round(len(flaky) / len(repeated), 4) if repeated else None,
    }


async def model_id(llm: httpx.AsyncClient, base_url: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    response = await llm.get(f"{base_url.rstrip('/')}/v1/models")
    response.raise_for_status()
    models = response.json().get("data") or []
    if not models:
        raise SystemExit("The LLM returned no model")
    return str(models[0]["id"])


def consolidate_into_sandbox(args: argparse.Namespace, sandbox: Path) -> dict[str, Any]:
    """Fold this round's lessons into the accumulated playbook the next round will serve.

    Runs out of process so the consolidator reads the same sandbox cache the usage batch just
    wrote, and so a consolidation crash cannot take the round loop down with it.
    """
    command = [
        sys.executable,
        str(ROOT / "eval" / "consolidate.py"),
        "--playbook",
        str(sandbox / "cache" / "playbook.json"),
        "--output",
        str(sandbox / "cache" / "playbook-candidate.json"),
        "--lesson-cap",
        str(args.lesson_cap),
    ]
    if args.no_llm:
        command.append("--no-llm")
    else:
        command += ["--llm-url", args.llm_url]
        if args.model:
            command += ["--model", args.model]
    completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, env={**os.environ, **args.env})
    if completed.returncode != 0:
        print(completed.stdout[-2000:])
        print(completed.stderr[-2000:], file=sys.stderr)
        return {"ok": False, "error": f"consolidate exited {completed.returncode}"}
    try:
        summary = json.loads(completed.stdout[completed.stdout.index("{") :])
    except (ValueError, json.JSONDecodeError):
        summary = {"ok": True, "raw": completed.stdout[-500:]}
    candidate = sandbox / "cache" / "playbook-candidate.json"
    if candidate.is_file():
        summary["promoted_to_served"] = True
    return summary


def promote_candidate(sandbox: Path) -> tuple[int, int]:
    """Make the candidate the playbook the next round serves, and sync the lesson store.

    The consolidator marks lessons active inside the document; without this the store still says
    pending, so the next round would try to re-promote them and the active count would read zero.
    """
    candidate = sandbox / "cache" / "playbook-candidate.json"
    served = sandbox / "cache" / "playbook.json"
    served.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
    document = Playbook.from_json(served.read_text(encoding="utf-8"))
    store = LessonStore(sandbox / "cache" / "lessons")
    active = 0
    for record in document.lessons:
        if store.set_status(record.lesson_id, "active") is not None:
            active += 1
    return active, len(document.lessons)


def read_holdout_ids(run_dir: Path) -> frozenset[str]:
    """Holdout membership as recorded by the usage batch.

    Raises instead of defaulting to empty. An unknown split would quietly return the holdout to the
    reflection set, which is the contamination this function exists to prevent, and a silent
    default is how that would have gone unnoticed the first time.
    """
    meta_path = run_dir / "run_meta.json"
    if not meta_path.is_file():
        raise SystemExit(f"{run_dir} has no run_meta.json, so its holdout membership is unknown")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if "holdout_ids" not in meta:
        raise SystemExit(f"{meta_path} predates recorded splits; re-run the batch without --reuse-run")
    return frozenset(str(case_id) for case_id in meta["holdout_ids"])


async def reflect_round(
    args: argparse.Namespace,
    settings: Settings,
    model: str,
    usages: list[CaseUsage],
    catalog: Catalog,
    holdout_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    selected = select_for_reflection(usages, args.max_reflections, holdout_ids)
    lessons: list[dict[str, Any]] = []
    for usage in selected:
        signatures = [StruggleSignature(**sig) for sig in usage.signatures]
        proposal, reason = await reflect(
            args.llm_url,
            model,
            usage.question,
            usage.events,
            usage.final_text,
            signatures,
            # Values the agent could not have seen: the exact leak test, computed here so the
            # oracle itself never reaches the reflector.
            forbidden=oracle_only_values(usage.expected, usage.seen),
            timeout=args.request_timeout,
        )
        if proposal is None:
            lessons.append(
                {
                    "case_id": usage.case_id,
                    "recorded": False,
                    "reason": reason,
                    "signatures": [sig["kind"] for sig in usage.signatures],
                }
            )
            continue
        if proposal.get("relation") and tuple(str(proposal["relation"]).split(".", 1)) not in catalog.relations:
            proposal["relation"] = None
        service = BenthicService(settings)
        report = await service.record_lesson(
            symptom=proposal["symptom"],
            lesson=proposal["lesson"],
            question_summary=usage.question,
            dataset=proposal.get("dataset"),
            relation=proposal.get("relation"),
            confidence=proposal.get("confidence", "medium"),
            # The gate measures a lesson against the case it came from, so the case has to travel
            # with it. Without this a lesson is unmeasurable and therefore never served.
            source_ref=usage.case_id,
        )
        await service.close()
        lessons.append(
            {
                "case_id": usage.case_id,
                "recorded": True,
                "lesson_id": report.lesson_id,
                "status": report.status,
                "reinforced": report.similar_pending > 0,
                "signatures": [sig["kind"] for sig in usage.signatures],
                **proposal,
                "warnings": report.warnings,
            }
        )
    return lessons


def run_attribution_gate(args: argparse.Namespace, sandbox: Path) -> dict[str, Any]:
    """Measure this round's new lessons before any of them can be served.

    Runs out of process because it drives eval/attrib.py, which itself drives the eval runner. It is
    the step that makes the loop honest: without it a lesson is promoted on the strength of being
    grounded and non-stale, and a store of true statements that change nothing is still a store of
    noise that costs prompt tokens on every turn.
    """
    command = [
        sys.executable,
        str(ROOT / "eval" / "attribute_pending.py"),
        "--sandbox",
        str(sandbox),
        "--playbook",
        str(sandbox / "cache" / "playbook.json"),
        "--attribution-dir",
        str(sandbox / "attribution"),
        "--reps",
        str(args.attribution_reps),
        "--max-turns",
        str(args.max_turns),
        "--llm-url",
        args.llm_url,
    ]
    if args.no_thinking:
        command += ["--no-thinking"]
    if args.model:
        command += ["--model", args.model]
    completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, env={**os.environ, **args.env})
    if completed.returncode != 0:
        print(completed.stdout[-2000:], file=sys.stderr)
        print(completed.stderr[-2000:], file=sys.stderr)
        return {"measured": [], "inherited": [], "error": f"gate exited {completed.returncode}"}
    try:
        return json.loads(completed.stdout[completed.stdout.index("{") :])
    except (ValueError, json.JSONDecodeError):
        return {"measured": [], "inherited": [], "error": "gate produced no verdict"}


def run_guard(args: argparse.Namespace, sandbox: Path) -> dict[str, Any]:
    """Tripwire the accumulated playbook against the last known good.

    Only run when the distilled core rules changed. Lessons are additive and low blast radius, but
    the always-on core competes for a hard token budget and changes every answer, so that is the
    point where a holdout check earns its cost.
    """
    command = [
        sys.executable,
        str(ROOT / "eval" / "guard.py"),
        "--sandbox",
        str(sandbox),
        "--reps",
        str(args.guard_reps),
        "--llm-url",
        args.llm_url,
    ]
    if args.no_llm:
        return {"ran": False, "reason": "llm disabled"}
    completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, env={**os.environ, **args.env})
    verdict: dict[str, Any] = {"ran": True, "exit_code": completed.returncode}
    for directory in sorted((sandbox / "guard-runs").glob("guard-*")):
        path = directory / "verdict.json"
        if path.is_file():
            try:
                verdict.update(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                continue
            verdict["report"] = str(directory / "report.md")
    if completed.returncode != 0:
        print(completed.stdout[-1500:], file=sys.stderr)
    return verdict


async def rounds_async(args: argparse.Namespace) -> int:
    sandbox = Path(args.sandbox)
    (sandbox / "runs").mkdir(parents=True, exist_ok=True)
    served = sandbox / "cache" / "playbook.json"
    args.env = {"BENTHIC_CACHE_DIR": str(sandbox / "cache"), "BENTHIC_PLAYBOOK_PATH": str(served)}
    os.environ.update(args.env)
    settings = Settings.from_env()

    async with httpx.AsyncClient(timeout=args.request_timeout) as llm:
        model = await model_id(llm, args.llm_url, args.model)
        service = BenthicService(settings)
        catalog: Catalog = (await service.playbook()).catalog
        await service.close()

        for index in range(args.rounds):
            round_number = args.start_round + index
            # Round 1 has no served playbook yet, so the server falls back to the curated seed.
            args.reuse_run = args.reuse_run if index == 0 else None
            run_dir, records = run_usage_batch(args, sandbox, str(served))
            holdout_ids = read_holdout_ids(run_dir)
            flip = rep_flip_rate(records)
            usages = [usage_from_result(record) for record in records]
            tuning = [usage for usage in usages if usage.case_id not in holdout_ids]
            tuning_passes = sum(usage.passed for usage in tuning)
            print(
                f"\n=== round {round_number}: {tuning_passes}/{len(tuning)} tuning strict"
                f" ({tuning_passes / max(1, len(tuning)):.1%})"
                f" | {len(usages) - len(tuning)} holdout case(s) withheld ===",
                flush=True,
            )
            if flip["flaky_share"] is not None:
                print(
                    f"  noise floor: {flip['flaky_share']:.0%} of repeated cases flip between reps"
                    f" ({len(flip['flaky_cases'])} of {len(flip['per_case'])})",
                    flush=True,
                )
            lessons = await reflect_round(args, settings, model, usages, catalog, holdout_ids)
            gate = (
                {"measured": [], "inherited": [], "skipped": True}
                if args.no_gate
                else run_attribution_gate(args, sandbox)
            )
            if gate.get("measured") or gate.get("inherited"):
                print(
                    f"  attribution gate: {len(gate['measured'])} measured,"
                    f" {len(gate['inherited'])} inherited from a measured twin,"
                    f" {len(gate.get('eligible_after_gate', []))} eligible,"
                    f" {len(gate.get('rejected', []))} rejected",
                    flush=True,
                )
            served_after_gate, _ = promote_candidate(sandbox)
            consolidation = consolidate_into_sandbox(args, sandbox)
            served_active, served_total = promote_candidate(sandbox)
            guard = {"ran": False, "reason": "core rules unchanged"}
            if consolidation.get("core_changed") and not args.no_guard:
                print("  core rules changed, running the holdout tripwire", flush=True)
                guard = run_guard(args, sandbox)
                if guard.get("rolled_back"):
                    # The guard restored the last known good playbook and quarantined the lessons
                    # that arrived with the bad batch; reload so the next round serves that.
                    served_active, served_total = promote_candidate(sandbox)

            lesson_store = LessonStore(sandbox / "cache" / "lessons")
            summary = {
                "round": round_number,
                "at": datetime.now(UTC).isoformat(),
                "usage_run": str(run_dir),
                "cases": len(usages),
                "tuning_cases": len(tuning),
                "tuning_passed": tuning_passes,
                "tuning_pass_rate": round(tuning_passes / max(1, len(tuning)), 4),
                "holdout_cases": sorted(holdout_ids),
                "rep_agreement": flip,
                "rediscovery": rediscovery(records),
                "attribution_gate": gate,
                "per_case": {usage.case_id: usage.passed for usage in usages},
                "tool_calls": dict(Counter(tool for usage in usages for tool in usage.tools)),
                "struggles": dict(Counter(sig["kind"] for usage in usages for sig in usage.signatures)),
                "failing_cases": sorted(usage.case_id for usage in usages if not usage.passed),
                "reflections_attempted": len(lessons),
                "reflection_reasons": dict(Counter(lesson.get("reason", "ok") for lesson in lessons)),
                "lessons_recorded": sum(1 for lesson in lessons if lesson.get("recorded")),
                "lessons": lessons,
                "consolidation": consolidation,
                "guard": guard,
                "active_lessons": len(lesson_store.by_status("active")),
                "pending_lessons": len(lesson_store.by_status("pending")),
                "served_lessons": served_total,
                "served_playbook": str(served),
            }
            (sandbox / f"round-{round_number}.json").write_text(
                json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
            )
            append_rounds(sandbox, summary)
            for lesson in lessons:
                if lesson.get("recorded"):
                    print(f"  lesson {lesson['lesson_id']} {lesson['lesson'][:110]}", flush=True)
            print(
                f"  active={summary['active_lessons']} pending={summary['pending_lessons']} "
                f"recorded={summary['lessons_recorded']}/{summary['reflections_attempted']}",
                flush=True,
            )
    return 0


def append_rounds(sandbox: Path, summary: dict[str, Any]) -> None:
    path = sandbox / "rounds.json"
    rounds = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    rounds.append(summary)
    path.write_text(json.dumps(rounds, indent=2, default=str) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", default=str(ROOT / "eval" / "harness"))
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--start-round", type=int, default=1)
    parser.add_argument("--lesson-cap", type=int, default=40)
    parser.add_argument("--guard-reps", type=int, default=2)
    parser.add_argument("--no-guard", action="store_true", help="never run the holdout tripwire")
    parser.add_argument("--llm-url", default="http://192.168.10.222:8081")
    parser.add_argument("--model")
    parser.add_argument("--split", choices=["all", "tuning", "holdout"], default="all")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--max-reflections", type=int, default=6)
    parser.add_argument(
        "--attribution-reps",
        type=int,
        default=3,
        help="repetitions per arm when measuring a lesson; below 3 an A/B cannot beat noise",
    )
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="skip the attribution gate, which lets unmeasured lessons into the served playbook",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="drive the agent with enable_thinking=false, which is how the suite is measured",
    )
    parser.add_argument("--request-timeout", type=float, default=300.0)
    parser.add_argument("--no-llm", action="store_true", help="skip the consolidator LLM pass")
    parser.add_argument("--case-filter")
    parser.add_argument("--reuse-run", default=None, help="reuse a previous run dir instead of re-running")
    raise SystemExit(asyncio.run(rounds_async(parser.parse_args())))


if __name__ == "__main__":
    main()
