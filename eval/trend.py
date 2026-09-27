"""Render the improvement trend from a sandbox's rounds.json.

Written to be read at a glance while a run is in progress, so it degrades gracefully when there
are only one or two rounds so far. Every number here is one repetition of the suite against a
stochastic model, so the pass-rate column is a direction, not a measurement.

Rounds are always re-scored with the current scorer rather than trusting the number recorded at run
time. A scorer bug silently understated five discovery cases during development, and a trend view
that repeats the recorded number would keep reporting the old, wrong figure.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "src"))


def load(sandbox: Path) -> list[dict[str, Any]]:
    path = sandbox / "rounds.json"
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []


def rescore(sandbox: Path) -> dict[str, dict[str, bool]] | None:
    """Recompute every round's per-case outcomes with the scorer as it stands now."""
    questions_path = ROOT / "eval" / "generated" / "questions.json"
    runs_dir = sandbox / "runs"
    if not questions_path.is_file() or not runs_dir.is_dir():
        return None
    try:
        from run_eval import score_case

        cases = {case["id"]: case for case in json.loads(questions_path.read_text(encoding="utf-8"))["cases"]}
    except (ImportError, ValueError, KeyError):
        return None

    out: dict[str, dict[str, bool]] = {}
    for run_dir in sorted(runs_dir.iterdir()):
        results_path = run_dir / "results.json"
        if not results_path.is_file():
            continue
        try:
            records = json.loads(results_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        outcomes: dict[str, bool] = {}
        for record in records:
            case = cases.get(record["id"])
            if case is None:
                continue
            outcomes[record["id"]] = score_case(
                case, record.get("events", []), record.get("final_text", ""), record.get("error", ""), strict=True
            )["passed"]
        if outcomes:
            out[run_dir.name] = outcomes
    return out or None


def attach_rescored(rounds: list[dict[str, Any]], sandbox: Path) -> list[dict[str, Any]]:
    """Overlay fresh per-case outcomes, counting tuning cases only.

    The holdout is excluded here for the same reason the harness excludes it from reflection: a
    playbook shaped by a case cannot then be credited or blamed for that case's score.
    """
    scored = rescore(sandbox)
    if not scored:
        return rounds
    out: list[dict[str, Any]] = []
    for record in rounds:
        run_name = Path(str(record.get("usage_run", ""))).name
        enriched = dict(record)
        outcomes = scored.get(run_name)
        if outcomes is not None:
            holdout = {str(case_id) for case_id in record.get("holdout_cases", [])}
            tuning = {case_id: ok for case_id, ok in outcomes.items() if case_id not in holdout}
            enriched["rescored_passed"] = sum(tuning.values())
            enriched["rescored_cases"] = len(tuning)
            enriched["rescored_failing"] = sorted(case_id for case_id, ok in tuning.items() if not ok)
        out.append(enriched)
    return out


def _passed(record: dict[str, Any]) -> int:
    return int(record.get("rescored_passed", record.get("tuning_passed", record.get("strict_passed", 0))))


def _cases(record: dict[str, Any]) -> int:
    return int(record.get("rescored_cases", record.get("tuning_cases", record.get("cases", 0))))


def failing_names(round_record: dict[str, Any]) -> list[str]:
    return [
        case.replace("join_usaspending_", "…")
        .replace("_usaspending_", "…")
        .replace("us_aspending_", "…")
        .replace("_samer_", "…")
        .replace("_usp_cl_", "…")
        .replace("_irs_ng_", "…")
        .replace("_up_cdmaps_", "…")
        .replace("discover_", "d:")
        .replace("sequential_", "seq:")
        for case in round_record.get("rescored_failing", round_record.get("failing_cases", []))
    ]


def render(rounds: list[dict[str, Any]]) -> str:
    if not rounds:
        return "No rounds recorded yet."
    lines = [
        "# Improvement trend",
        "",
        "Tuning cases only, re-scored with the current scorer. The holdout is withheld from both",
        "reflection and this table, so these numbers describe cases the playbook was not built from.",
        "",
        "| round | re-scored | as recorded | failing | active lessons | new | recorded | top struggles |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in rounds:
        cases = _cases(record)
        passed = _passed(record)
        struggles = sorted(record.get("struggles", {}).items(), key=lambda item: -item[1])[:3]
        lines.append(
            "| {round} | {passed}/{cases} ({rate:.0%}) | {original}/{cases} | {failing} | {active} | {new} | {recorded}/{attempted} | {struggles} |".format(
                round=record.get("round"),
                passed=passed,
                cases=cases,
                rate=passed / cases if cases else 0.0,
                original=record.get("tuning_passed", record.get("strict_passed", 0)),
                failing=len(record.get("rescored_failing", record.get("failing_cases", []))),
                active=record.get("active_lessons", 0),
                new=record.get("served_lessons", 0) - _previous_active(rounds, record),
                recorded=record.get("lessons_recorded", 0),
                attempted=record.get("reflections_attempted", 0),
                struggles=", ".join(f"{kind} {count}" for kind, count in struggles) or "-",
            )
        )

    first, last = rounds[0], rounds[-1]
    if len(rounds) > 1:
        delta = _passed(last) - _passed(first)
        lines += [
            "",
            f"Change over {len(rounds)} rounds: "
            f"{_passed(first)}/{_cases(first)} -> {_passed(last)}/{_cases(last)} ({delta:+d} cases).",
        ]
        best_score = max(_passed(record) for record in rounds)
        last_score = _passed(last)
        best_rounds = [record["round"] for record in rounds if _passed(record) == best_score]
        if last_score < best_score:
            lines.append(
                f"Best was {best_score} at round(s) {', '.join(str(r) for r in best_rounds)}. The current "
                f"playbook is below that, which is the case `eval/guard.py` rolls back."
            )
        elif len(best_rounds) > 1 and last_score == best_score:
            lines.append(
                f"The current playbook ties the best score ({best_score}), reached at rounds "
                f"{', '.join(str(r) for r in best_rounds)}."
            )

    always_failing = sorted(
        set.intersection(*(set(record.get("rescored_failing", record.get("failing_cases", []))) for record in rounds))
    )
    if len(rounds) > 1 and always_failing:
        lines += ["", "Failing in every round (accumulation has not fixed these yet):"]
        lines += [f"- {case}" for case in always_failing]

    last = rounds[-1]
    if _cases(last) and _passed(last) == _cases(last):
        lines += [
            "",
            "**The suite is saturated.** The last round passed every case, so it can no longer detect",
            "improvement: further rounds will only measure noise around 100%. Either accept that the",
            "remaining job is not regressing, handled by `eval/guard.py`, or add harder cases so there",
            "is headroom to improve into. Generating a tougher split is `eval/generate_cases.py`.",
        ]

    lines += [
        "",
        "Each round is one repetition of the suite against a stochastic model, so treat the pass-rate",
        "column as a direction rather than a measurement. A single case flipping is within the noise",
        "observed on this model. `eval/guard.py` is the check that would catch accumulation making",
        "things worse; the harness runs it automatically whenever the distilled core rules change.",
    ]
    return "\n".join(lines) + "\n"


def _previous_active(rounds: list[dict[str, Any]], record: dict[str, Any]) -> int:
    index = next((i for i, item in enumerate(rounds) if item.get("round") == record.get("round")), 0)
    return rounds[index - 1].get("active_lessons", 0) if index else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", default=str(ROOT / "eval" / "harness"))
    parser.add_argument("--failing", action="store_true", help="also list the currently failing cases")
    args = parser.parse_args()

    sandbox = Path(args.sandbox)
    rounds = attach_rescored(load(sandbox), sandbox)
    print(render(rounds))
    if args.failing and rounds:
        print("Currently failing:")
        for case in failing_names(rounds[-1]):
            print(f"- {case}")


if __name__ == "__main__":
    main()
