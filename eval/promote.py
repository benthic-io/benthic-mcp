"""A/B gate for promoting a candidate playbook.

Runs the real MCP tool surface twice against the same LLM, once with the current playbook and
once with the candidate, and only promotes when the candidate is measurably better. The
tuning/holdout split exists so a playbook cannot simply memorise the cases it was tuned on:
improvements must also hold on cases the consolidator never saw fail.

Every threshold here is a refusal. A candidate that is merely not-worse is rejected, because
serving extra guidance to every future caller for no measured benefit is a bad trade.
"""

import argparse
import asyncio
import json
import math
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from benthic_mcp.bdp import BdpRepository  # noqa: E402
from benthic_mcp.catalog import Catalog  # noqa: E402
from benthic_mcp.config import Settings  # noqa: E402
from benthic_mcp.playbook import Playbook, verify  # noqa: E402
from benthic_mcp.trace import LessonStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

MAX_TOOL_CALL_GROWTH = 0.10
MAX_LATENCY_GROWTH = 0.20
MIN_PASS_RATE_GAIN = 0.0


@dataclass(slots=True)
class Arm:
    name: str
    run_dir: Path
    passed: int = 0
    total: int = 0
    per_case: dict[str, bool] = field(default_factory=dict)
    tool_calls: list[int] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    forbidden_hits: dict[str, list[str]] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def mean_calls(self) -> float:
        return mean(self.tool_calls) if self.tool_calls else 0.0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
        return ordered[index]


def load_arm(name: str, run_dir: Path) -> Arm:
    arm = Arm(name=name, run_dir=run_dir)
    for record in json.loads((run_dir / "results.json").read_text(encoding="utf-8")):
        passed = bool(record["score"]["passed"])
        arm.per_case[record["id"]] = passed
        arm.total += 1
        arm.passed += int(passed)
        arm.tool_calls.append(len(record.get("events", [])))
        arm.latencies.append(float(record.get("elapsed_ms", 0)))
        hits = record["score"].get("forbidden_claim_hits") or []
        if hits:
            arm.forbidden_hits[record["id"]] = list(hits)
    return arm


def evaluate(baseline: Arm, candidate: Arm) -> dict[str, Any]:
    regressions = sorted(
        case for case, passed in baseline.per_case.items() if passed and not candidate.per_case.get(case, False)
    )
    new_hits = sorted(
        case
        for case, hits in candidate.forbidden_hits.items()
        if len(hits) > len(baseline.forbidden_hits.get(case, []))
    )
    call_growth = (candidate.mean_calls - baseline.mean_calls) / baseline.mean_calls if baseline.mean_calls else 0.0
    latency_growth = (
        (candidate.p95_latency - baseline.p95_latency) / baseline.p95_latency if baseline.p95_latency else 0.0
    )
    gain = candidate.pass_rate - baseline.pass_rate

    checks = [
        {
            "name": "pass_rate_improves",
            "ok": gain > MIN_PASS_RATE_GAIN,
            "detail": f"{baseline.pass_rate:.2f} -> {candidate.pass_rate:.2f}",
        },
        {"name": "no_case_regresses", "ok": not regressions, "detail": ", ".join(regressions) or "none"},
        {"name": "no_new_forbidden_claims", "ok": not new_hits, "detail": ", ".join(new_hits) or "none"},
        {
            "name": "tool_calls_within_budget",
            "ok": call_growth <= MAX_TOOL_CALL_GROWTH,
            "detail": f"{baseline.mean_calls:.2f} -> {candidate.mean_calls:.2f} ({call_growth:+.1%})",
        },
        {
            "name": "p95_latency_within_budget",
            "ok": latency_growth <= MAX_LATENCY_GROWTH,
            "detail": f"{baseline.p95_latency:.0f}ms -> {candidate.p95_latency:.0f}ms ({latency_growth:+.1%})",
        },
    ]
    return {
        "passed": all(check["ok"] for check in checks),
        "checks": checks,
        "regressions": regressions,
        "new_forbidden_claims": new_hits,
        "pass_rate_gain": gain,
        "tool_call_growth": call_growth,
        "latency_growth": latency_growth,
    }


async def run_arm(playbook: str | None, split: str, args: argparse.Namespace, label: str) -> Arm:
    command = [
        sys.executable,
        str(ROOT / "eval" / "run_eval.py"),
        "--in-process",
        "--split",
        split,
        "--llm-url",
        args.llm_url,
        "--max-turns",
        str(args.max_turns),
        "--output-dir",
        str(args.output_dir),
    ]
    if playbook:
        command += ["--playbook", playbook]
    if args.model:
        command += ["--model", args.model]
    if args.case_filter:
        command += ["--case-filter", args.case_filter]

    completed = await asyncio.to_thread(subprocess.run, command, cwd=str(ROOT), capture_output=True, text=True)
    if completed.returncode != 0:
        print(completed.stdout[-4000:])
        print(completed.stderr[-4000:], file=sys.stderr)
        raise SystemExit(f"{label} run failed with exit code {completed.returncode}")

    runs = sorted((Path(args.output_dir)).iterdir())
    run_dir = runs[-1]
    print(f"{label}: {run_dir}")
    return load_arm(label, run_dir)


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, follow_redirects=False) as client:
        catalog = Catalog(await BdpRepository(settings, client).load())

    active_path = args.active or (settings.cache_dir / "playbook.json")
    candidate_path = args.candidate or (settings.cache_dir / "playbook-candidate.json")
    if not candidate_path.is_file():
        raise SystemExit(f"No candidate at {candidate_path}; run eval/consolidate.py first")

    candidate = Playbook.from_json(candidate_path.read_text(encoding="utf-8"))
    verified, report = verify(candidate, catalog)
    if report.total_dropped:
        print("Candidate failed catalog verification:", json.dumps(report.as_dict(), indent=2))
        return 1

    baseline_playbook = str(active_path) if active_path.is_file() else "seed"
    previous_fingerprint = (
        Playbook.from_json(active_path.read_text(encoding="utf-8")).catalog_fingerprint
        if active_path.is_file()
        else None
    )
    print(f"Baseline playbook: {baseline_playbook}")
    print(f"Candidate playbook: {candidate_path}")

    baseline = await run_arm(baseline_playbook, "tuning", args, "baseline-tuning")
    candidate_arm = await run_arm(str(candidate_path), "tuning", args, "candidate-tuning")
    tuning = evaluate(baseline, candidate_arm)

    holdout: dict[str, Any] = {"ran": False}
    if tuning["passed"]:
        baseline_holdout = await run_arm(baseline_playbook, "holdout", args, "baseline-holdout")
        candidate_holdout = await run_arm(str(candidate_path), "holdout", args, "candidate-holdout")
        holdout = evaluate(baseline_holdout, candidate_holdout)
        holdout["ran"] = True
        # The holdout is a veto, not a target: a candidate may not trade holdout accuracy for
        # tuning accuracy.
        holdout_ok = holdout["no_case_regresses"] and holdout["pass_rate_gain"] >= 0
    else:
        holdout_ok = False

    promoted = tuning["passed"] and holdout_ok
    verdict = {
        "promoted": promoted,
        "candidate": str(candidate_path),
        "active": str(active_path),
        "verification_drops": report.as_dict(),
        "tuning": tuning,
        "holdout": holdout,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    print(json.dumps(verdict, indent=2, default=str))

    report_dir = ROOT / "eval" / "runs" / f"promotion-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "verdict.json").write_text(json.dumps(verdict, indent=2, default=str) + "\n", encoding="utf-8")

    if not promoted:
        return 1

    new_lessons = [record.lesson_id for record in verified.lessons if record.status == "active"]
    promoted_document = verified.model_copy(
        update={
            "generated_at": datetime.now(UTC),
            "baseline_fingerprint": previous_fingerprint,
            "baseline_run_id": str(baseline.run_dir.name),
        }
    )
    active_path.parent.mkdir(parents=True, exist_ok=True)
    if active_path.is_file():
        previous = active_path.with_suffix(f".{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.bak")
        previous.write_text(active_path.read_text(encoding="utf-8"), encoding="utf-8")
    active_path.write_text(promoted_document.to_json(), encoding="utf-8")

    store = LessonStore(settings.cache_dir / "lessons")
    for lesson_id in new_lessons:
        store.set_status(lesson_id, "active")

    print(f"Promoted {len(new_lessons)} lessons to {active_path}. Run: systemctl --user restart benthic-mcp.service")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=None)
    parser.add_argument("--active", type=Path, default=None)
    parser.add_argument("--output-dir", default=str(ROOT / "eval" / "runs"))
    parser.add_argument("--llm-url", default="http://192.168.10.222:8081")
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--case-filter")
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
