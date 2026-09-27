"""Holdout tripwire for the accumulated playbook.

Accumulation is unblocked by design: verified lessons are merged in rather than gated on a
pass-rate gain, because most individual lessons do not move a 24-case pass rate and a strict gate
would stop all learning. That makes regression the thing that actually needs catching, so this is
a tripwire, not a measurement of improvement.

Two honest limits, stated in the verdict it prints:

1. Five holdout cases at two reps cannot resolve a small gain. It detects gross damage and nothing
   subtler.
2. The server never sees the agent's final answer, so a confidently wrong answer with clean tool
   calls produces no struggle signature and no lesson. This is the main class of error the tripwire
   exists to catch.

Rollback is per lesson, not per playbook: the lessons added since the last known-good are
quarantined individually so one bad correction does not discard everything learned since.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from benthic_mcp.playbook import Playbook  # noqa: E402
from benthic_mcp.seed import seed_playbook  # noqa: E402
from benthic_mcp.trace import LessonStore  # noqa: E402

HOLDOUT = "holdout"


@dataclass(slots=True)
class Arm:
    name: str
    playbook: str
    per_case: dict[str, list[bool]] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)

    @property
    def passes(self) -> int:
        return sum(1 for outcomes in self.per_case.values() for outcome in outcomes if outcome)

    @property
    def total(self) -> int:
        return sum(len(outcomes) for outcomes in self.per_case.values())

    def rate_for(self, case_id: str) -> str:
        outcomes = self.per_case.get(case_id, [])
        if not outcomes:
            return "n/a"
        return f"{sum(outcomes)}/{len(outcomes)}"


def run_arm(args: argparse.Namespace, name: str, playbook: str, env: dict[str, str]) -> Arm:
    command = [
        sys.executable,
        str(ROOT / "eval" / "run_eval.py"),
        "--in-process",
        "--strict",
        "--split",
        HOLDOUT,
        "--reps",
        str(args.reps),
        "--llm-url",
        args.llm_url,
        "--max-turns",
        str(args.max_turns),
        "--output-dir",
        str(args.output_dir),
        "--playbook",
        playbook,
    ]
    if args.model:
        command += ["--model", args.model]
    completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, env={**os.environ, **env})
    if completed.returncode != 0:
        print(completed.stdout[-3000:], file=sys.stderr)
        print(completed.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"{name} arm failed with exit code {completed.returncode}")

    run_dir = sorted(Path(args.output_dir).iterdir())[-1]
    arm = Arm(name=name, playbook=playbook)
    for record in json.loads((run_dir / "results.json").read_text(encoding="utf-8")):
        arm.per_case.setdefault(record["id"], []).append(bool(record["score"]["passed"]))
        arm.calls.append(len(record.get("events", [])))
        arm.latencies.append(float(record.get("elapsed_ms", 0)))
    print(f"{name}: {arm.passes}/{arm.total} holdout  ({run_dir.name})", flush=True)
    return arm


def compare(baseline: Arm, candidate: Arm) -> dict[str, Any]:
    """Decide whether the candidate damaged the holdout.

    A single case losing one rep out of two is noise, not damage. With so few cases and reps,
    quarantining lessons on that signal would throw away good corrections far more often than it
    would catch a bad one, so a regression needs corroboration: either the aggregate dropped and at
    least two cases got worse, or one case collapsed outright from fully passing to fully failing.
    """
    regressions: list[dict[str, Any]] = []
    collapses: list[dict[str, Any]] = []
    for case_id, outcomes in sorted(candidate.per_case.items()):
        before = baseline.per_case.get(case_id, [])
        if not before or sum(outcomes) >= sum(before):
            continue
        entry = {
            "case": case_id,
            "known_good": baseline.rate_for(case_id),
            "candidate": candidate.rate_for(case_id),
        }
        regressions.append(entry)
        if not any(outcomes) and all(before):
            collapses.append(entry)

    delta = candidate.passes - baseline.passes
    aggregate_dropped = delta < 0
    regressed = (aggregate_dropped and len(regressions) >= 2) or bool(collapses)

    # A holdout that every arm passes outright carries no information. Treating that as "no
    # regression" and advancing known-good is how a degraded playbook gets blessed, so it is
    # reported as inconclusive instead.
    saturated = bool(baseline.total) and baseline.passes == baseline.total and candidate.passes == candidate.total

    return {
        "regressed": regressed,
        "regressions": regressions,
        "collapses": collapses,
        "aggregate_dropped": aggregate_dropped,
        "saturated": saturated,
        "pass_rate_delta": round(delta / max(1, candidate.total), 4),
        "per_case": {
            case_id: {"known_good": baseline.rate_for(case_id), "candidate": candidate.rate_for(case_id)}
            for case_id in sorted(candidate.per_case)
        },
    }


def render(verdict: dict[str, Any]) -> str:
    lines = [
        "# Holdout tripwire",
        "",
        f"- decided at: {verdict['decided_at']}",
        f"- known good: {verdict['known_good']}",
        f"- candidate:  {verdict['candidate']}",
        f"- outcome:    {verdict['outcome']}",
        f"- quarantined lessons: {len(verdict['quarantined_lessons'])}",
        f"- baseline seeded from the seed playbook: {verdict.get('seeded_baseline', False)}",
        "",
        "| holdout case | known good | candidate |",
        "| --- | --- | --- |",
    ]
    for case_id, rates in verdict["comparison"]["per_case"].items():
        marker = (
            " **regressed**" if any(item["case"] == case_id for item in verdict["comparison"]["regressions"]) else ""
        )
        lines.append(f"| {case_id}{marker} | {rates['known_good']} | {rates['candidate']} |")
    if verdict["comparison"].get("saturated"):
        lines += [
            "",
            "**The holdout is saturated: both arms passed every case.** That is an uninformative",
            "measurement, not a clean result. Known good was deliberately not advanced, and no",
            "rollback was performed, because this run cannot tell a good playbook from a bad one.",
            "The holdout needs harder cases before it can guard anything.",
        ]
    lines += [
        "",
        "This is a tripwire against gross regression, not a measurement of improvement. With a",
        "handful of holdout cases it cannot resolve a small gain, and it is the only check that can",
        "catch a confidently wrong answer with a clean tool trace, which produces no lesson.",
    ]
    if verdict["quarantined_lessons"]:
        lines += ["", "Quarantined:", *[f"- {lesson_id}" for lesson_id in verdict["quarantined_lessons"]]]
    return "\n".join(lines) + "\n"


def lessons_since(known_good: Playbook, candidate: Playbook) -> list[str]:
    before = {record.lesson_id for record in known_good.lessons}
    return [record.lesson_id for record in candidate.lessons if record.lesson_id not in before]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", default=str(ROOT / "eval" / "harness"))
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--llm-url", default="http://192.168.10.222:8081")
    parser.add_argument("--model")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--accept", action="store_true", help="record the current playbook as known good without a comparison"
    )
    parser.add_argument("--dry-run", action="store_true", help="report the comparison but never roll back")
    args = parser.parse_args()

    cache = Path(args.sandbox) / "cache"
    served_path = cache / "playbook.json"
    good_path = cache / "playbook.known-good.json"
    if not served_path.is_file():
        raise SystemExit(f"No accumulated playbook at {served_path}; run eval/harness.py first")
    if args.output_dir is None:
        args.output_dir = str(Path(args.sandbox) / "guard-runs")
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    env = {"BENTHIC_CACHE_DIR": str(cache), "BENTHIC_PLAYBOOK_PATH": str(served_path)}

    if args.accept:
        shutil.copyfile(served_path, good_path)
        print(f"Recorded known good: {good_path}")
        return

    seeded = False
    if not good_path.is_file():
        # The seed is the honest baseline on a first run, not the freshly consolidated candidate.
        # Copying the candidate over instead, as this used to, blessed the change that the guard
        # was supposed to be checking, and the round's core rules were never measured.
        good_path.write_text(seed_playbook().to_json(), encoding="utf-8")
        seeded = True
        print(f"No known good on record; using the curated seed as the baseline: {good_path}")

    known_good = Playbook.from_json(good_path.read_text(encoding="utf-8"))
    candidate = Playbook.from_json(served_path.read_text(encoding="utf-8"))
    added = lessons_since(known_good, candidate)

    baseline = run_arm(args, "known-good", str(good_path), env)
    challenger = run_arm(args, "candidate", str(served_path), env)
    comparison = compare(baseline, challenger)

    saturated = comparison["saturated"]
    detected = comparison["regressed"]
    rolled_back = False
    quarantined: list[str] = []
    if detected and args.dry_run:
        print("Regression detected, but --dry-run suppressed the rollback.")
    elif detected:
        # Roll the document back first, then quarantine only the lessons that arrived since.
        shutil.copyfile(good_path, served_path)
        store = LessonStore(cache / "lessons")
        for lesson_id in added:
            if store.set_status(lesson_id, "quarantined") is not None:
                quarantined.append(lesson_id)
        rolled_back = True

    if rolled_back:
        outcome = "ROLLED BACK"
    elif detected:
        outcome = "REGRESSION DETECTED (dry run, no action taken)"
    elif saturated:
        outcome = "INCONCLUSIVE (holdout saturated)"
    else:
        outcome = "HELD"

    verdict = {
        "decided_at": datetime.now(UTC).isoformat(),
        "known_good": str(good_path),
        "candidate": str(served_path),
        "comparison": comparison,
        "lessons_added_since_known_good": added,
        "quarantined_lessons": quarantined,
        "rolled_back": rolled_back,
        "regression_detected": detected,
        "held": not detected and not saturated,
        "outcome": outcome,
        "seeded_baseline": seeded,
        "dry_run": args.dry_run,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir = Path(args.output_dir) / f"guard-{stamp}"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "verdict.json").write_text(json.dumps(verdict, indent=2, default=str) + "\n", encoding="utf-8")
    report = render(verdict)
    (report_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    if saturated:
        print("Holdout saturated in both arms: this run is uninformative. Known good was not advanced.")
    elif args.dry_run:
        print("Dry run: known good was not advanced.")
    elif not detected:
        shutil.copyfile(served_path, good_path)
        print(f"No regression; recorded the current playbook as known good: {good_path}")
    # A rollback is the one outcome worth failing a shell pipeline over.
    raise SystemExit(1 if rolled_back else 0)


if __name__ == "__main__":
    main()
