"""Decide whether a rule belongs in the always-on core, by measuring the whole tuning split.

Per-lesson attribution answers a narrow question: does this advice fix the case it came from. That
instrument is biased against general advice, and the bias is not hypothetical: a rule can help five
different cases by one case each and nothing in particular on any single one of them, which no
single-case comparison can see.

This is the instrument that can. It exists partly because the obvious candidate for it turned out not
to need it: the answer-delivery rule, which this was built to justify, measured 23/50 with and 23/50
without over two repetitions of the tuning split, and was removed from the seed core. A rule reaches
that core by being measured here, not by being written down.

So the loop needs a second instrument, and this is it: put the rule in the always-on core, run the
whole tuning split with and without it, and call it on the aggregate. The always-on core is also the
only channel that measurably works. Moving the same advice from the core into the on-demand lesson
store showed no gain and sat inside the capabilities the suite is least stable in, and three of its
four regressions never called benthic_playbook at all, so the content was not what misled them.

Two things this deliberately does not do.

It does not use the holdout. The split argument is required to be the tuning subset, and the loader
refuses anything containing a holdout case, because a rule chosen on holdout performance is a rule
tuned on the answer key.

It does not run per lesson per round. One comparison is two full runs of the tuning split, so this
belongs to a rule that has already survived per-case scrutiny and looks general, not to the ordinary
accumulation path. `--min-delta` is set so a single case cannot carry a verdict on its own, because
at this size the suite moves by one or two cases between identical runs.

Usage:

    python eval/attribute_suite.py --rule "Never end the turn without a final answer." --reps 1
    python eval/attribute_suite.py --lesson-id 7462c096217e46cc --reps 1
"""

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "src"))

import core_evidence  # noqa: E402

from benthic_mcp.playbook import load_playbook  # noqa: E402
from benthic_mcp.seed import seed_playbook  # noqa: E402

# One case out of twenty-five is inside the run-to-run spread this suite already shows, so a verdict
# has to rest on more than that to mean anything.
MIN_DELTA = 2


def tuning_split(path: Path) -> list[str]:
    """The tuning case ids of the real suite, used only to confirm the selection is not empty.

    Selecting the split in the runner rather than by writing a filtered file matters: `assign_splits`
    takes one holdout case per capability, so it is not stable under subsetting and a filtered file
    would yield a different holdout than the one the rest of the harness uses.
    """
    from run_eval import assign_splits

    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    holdout = assign_splits(cases)
    tuning = [str(case["id"]) for case in cases if str(case["id"]) not in holdout]
    if not tuning or not holdout:
        raise SystemExit(f"{path} yields an empty split; there is nothing to measure")
    return tuning


def rule_text(args: argparse.Namespace) -> str:
    if args.rule:
        return args.rule.strip()
    document, status = load_playbook(Path(args.playbook))
    if document is None:
        raise SystemExit(f"{args.playbook} did not load ({status})")
    match = [record for record in document.lessons if record.lesson_id == args.lesson_id]
    if not match:
        raise SystemExit(f"no lesson {args.lesson_id} in {args.playbook}")
    return match[0].lesson.strip()


def write_arm(path: Path, base: Path, rule: str | None) -> None:
    """One arm's playbook: the base with the rule as an always-on core line, or without it."""
    document = load_playbook(base)[0] if base.is_file() else seed_playbook()
    if document is None:
        raise SystemExit(f"{base} did not load")
    core = [line for line in document.core if line != rule] if rule else list(document.core)
    if rule:
        core = [*core, rule]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document.model_copy(update={"core": core}).to_json(), encoding="utf-8")


def arm_command(args: argparse.Namespace, label: str, playbook: Path, questions: Path) -> list[str]:
    """One arm's runner invocation. `--split tuning` is what keeps the holdout out."""
    command = [
        sys.executable,
        str(ROOT / "eval" / "run_eval.py"),
        "--questions",
        str(questions),
        "--split",
        "tuning",
        "--reps",
        str(args.reps),
        "--strict",
        "--in-process",
        "--playbook",
        str(playbook),
        "--max-turns",
        str(args.max_turns),
        "--llm-url",
        args.llm_url,
        "--output-dir",
        str(Path(args.workdir) / label),
    ]
    if args.no_thinking:
        command += ["--no-thinking"]
    if args.model:
        command += ["--model", args.model]
    return command


def run_arm(args: argparse.Namespace, label: str, playbook: Path, questions: Path) -> dict[str, Any]:
    """Run one arm and read its results from the run directory.

    The report is read from disk rather than scraped out of stdout, because run_eval prints a progress
    line per case, `[3/25] case_id`, and the first `[` in its output belongs to one of those rather
    than to the report.
    """
    out_root = Path(args.workdir) / label
    completed = subprocess.run(
        arm_command(args, label, playbook, questions), cwd=str(ROOT), capture_output=True, text=True
    )
    if completed.returncode != 0:
        print(completed.stdout[-2000:], file=sys.stderr)
        print(completed.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"the {label} arm failed")
    runs = sorted(path for path in out_root.iterdir() if (path / "results.json").is_file())
    if not runs:
        raise SystemExit(f"the {label} arm wrote no results.json under {out_root}")
    results = json.loads((runs[-1] / "results.json").read_text(encoding="utf-8"))
    passed = {record["id"]: record["score"]["passed"] for record in results}
    return {
        "label": label,
        "playbook": str(playbook),
        "run": runs[-1].name,
        "cases": len(results),
        "passed": sum(passed.values()),
        "per_case": passed,
        "empty_answers": sum(1 for record in results if not record.get("final_text", "").strip()),
        "max_turns": sum(1 for record in results if record.get("error") == "maximum turns reached"),
        "median_ms": round(statistics.median(record["elapsed_ms"] for record in results)),
    }


def judge(baseline: dict[str, Any], candidate: dict[str, Any], min_delta: int) -> str:
    """Aggregate verdict, and a check that the comparison is not reading server drift."""
    gained = [cid for cid, ok in candidate["per_case"].items() if ok and not baseline["per_case"].get(cid)]
    lost = [cid for cid, ok in baseline["per_case"].items() if ok and not candidate["per_case"].get(cid)]
    delta = len(gained) - len(lost)
    # A wedged llama-server shows up as a run that takes minutes, not as a case that flips. Two
    # full runs at different moments is exactly when that happens, so it is checked rather than
    # assumed away.
    slower = max(baseline["median_ms"], candidate["median_ms"]) / max(
        1, min(baseline["median_ms"], candidate["median_ms"])
    )
    if slower >= 2.0:
        return "unreadable: one arm ran more than twice as slowly, so a server problem is likelier than a rule"
    if delta >= min_delta:
        return "fixes"
    if delta <= -min_delta:
        return "regresses"
    return "no_effect"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule", help="the rule text to try as an always-on core line")
    parser.add_argument("--lesson-id", help="take the text from this lesson in the served playbook")
    parser.add_argument("--playbook", default=str(ROOT / "eval" / "harness" / "cache" / "playbook.json"))
    parser.add_argument("--questions", default=str(ROOT / "eval" / "generated" / "questions.json"))
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--min-delta", type=int, default=MIN_DELTA)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--llm-url", default="http://192.168.10.222:8081")
    parser.add_argument("--model")
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument("--workdir", default=str(ROOT / "eval" / "attrib-suite"))
    parser.add_argument("--output", default=str(ROOT / "eval" / "attrib-suite" / "report.json"))
    parser.add_argument(
        "--record",
        nargs="?",
        const=str(ROOT / "eval" / "core-evidence.json"),
        help="record the verdict in this file, which is what lets a passing rule reach the core",
    )
    args = parser.parse_args()
    if not args.rule and not args.lesson_id:
        parser.error("pass --rule or --lesson-id")

    tuning_split(Path(args.questions))
    rule = rule_text(args)
    with_arm, without_arm = Path(args.workdir) / "with.json", Path(args.workdir) / "without.json"
    write_arm(with_arm, Path(args.playbook), rule)
    write_arm(without_arm, Path(args.playbook), None)

    baseline = run_arm(args, "without", without_arm, Path(args.questions))
    candidate = run_arm(args, "with", with_arm, Path(args.questions))
    report = {
        "rule": rule,
        "reps": args.reps,
        "min_delta": args.min_delta,
        "baseline": baseline,
        "candidate": candidate,
        "gained": [cid for cid, ok in candidate["per_case"].items() if ok and not baseline["per_case"].get(cid)],
        "lost": [cid for cid, ok in baseline["per_case"].items() if ok and not candidate["per_case"].get(cid)],
    }
    report["delta"] = len(report["gained"]) - len(report["lost"])
    report["verdict"] = judge(baseline, candidate, args.min_delta)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.record:
        # Recording is what lets the verdict change anything. Without it the report is a file nobody
        # reads and the rule cannot join the always-on core, however well it measured.
        entry = core_evidence.record(Path(args.record), Path(args.output))
        print(f"recorded in {args.record} as {entry.verdict} ({entry.baseline} -> {entry.candidate})")
    print(json.dumps({k: v for k, v in report.items() if k not in ("baseline", "candidate")}, indent=2))
    print(f"\n  without: {baseline['passed']}/{baseline['cases']}  with: {candidate['passed']}/{candidate['cases']}")
    return 0 if report["verdict"] == "fixes" else 1


if __name__ == "__main__":
    raise SystemExit(main())
