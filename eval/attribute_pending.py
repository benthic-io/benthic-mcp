"""Measure every pending lesson against the case it came from, so promotion requires evidence.

This is the step the loop was missing. A lesson was promoted on the strength of two things: the
catalog said it was grounded, and the catalog fingerprint matched. Both are correctness checks.
Neither says the advice changes anything, and the measurement said the opposite - twelve grounded
lessons scored identically to one hand-written rule, and the core slice filled with restated advice
that cost prompt tokens on every turn.

So a lesson now has to be attributed before it can be served. The gate is deliberately cheap to run
and honest about its limits:

- A lesson whose advice a measured lesson already covers inherits that verdict, with the donor
  recorded, rather than being measured again. The reflector paraphrases, so the same advice arrives
  as a new record every round and would otherwise cost an A/B per round forever.
- Anything that comes back `no_effect`, `inconclusive` or `regresses` is not promoted, and a
  `regresses` verdict is quarantined outright rather than left pending to be re-measured.
- A lesson with no recorded source case cannot be measured and is left untested. That is the
  honest outcome, not a silent pass.

Cost is the constraint that shapes this. One A/B is two cases at N repetitions, so a round's new
lessons are the only thing measured here; nothing already measured is re-measured.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "src"))

from attrib import without_lesson  # noqa: E402
from harness import promote_candidate  # noqa: E402

from benthic_mcp.playbook import load_playbook  # noqa: E402
from benthic_mcp.trace import LessonStore  # noqa: E402

# Below this, an A/B cannot distinguish the lesson from noise, and recording "fixes" would be a
# claim the data does not support.
MEANINGFUL_REPS = 3


@dataclass(slots=True)
class GateResult:
    measured: list[str]
    inherited: list[str]
    promoted: list[str]
    rejected: list[tuple[str, str]]
    quarantined: list[str]
    unmeasurable: list[str]

    def as_json(self) -> dict[str, Any]:
        return {
            "measured": self.measured,
            "inherited": self.inherited,
            "eligible_after_gate": self.promoted,
            "rejected": [{"lesson_id": lesson_id, "verdict": verdict} for lesson_id, verdict in self.rejected],
            "quarantined": self.quarantined,
            "unmeasurable": self.unmeasurable,
        }


def source_case(record: Any) -> str | None:
    """The case id a lesson was learned from, if the reporter supplied one."""
    return record.source_ref or None


def holdout_cases(questions_path: str) -> set[str]:
    """The holdout split, so a lesson can never be measured against it.

    Measuring a lesson on a holdout case would be training on the holdout through the back door, and
    three of the twelve accumulated lessons were learned from holdout cases. The gate has to be able
    to see that, or it will happily measure them and then trust the result.
    """
    from run_eval import assign_splits

    cases = json.loads(Path(questions_path).read_text(encoding="utf-8"))["cases"]
    return assign_splits(cases)


def apply_inherited(store: LessonStore, record: Any) -> bool:
    """Give a near-duplicate of a measured lesson that lesson's verdict, with the donor recorded."""
    donor = store.inheritable(record)
    if donor is None:
        return False
    store.set_attribution(
        record.lesson_id,
        donor.attribution,
        case_id=donor.attribution_case,
        reps=donor.attribution_reps,
        inherited_from=donor.lesson_id,
    )
    return True


def import_measured(store: LessonStore, lesson_id: str, verdict: dict[str, Any], min_reps: int) -> None:
    """Fold an `eval/attrib.py` report into the store."""
    case_id = verdict["case"]
    reps = int(verdict["reps"])
    if reps < min_reps:
        verdict_name = "inconclusive"
    else:
        verdict_name = str(verdict["results"][case_id]["verdict"])
    store.set_attribution(lesson_id, verdict_name, case_id=case_id, reps=reps)


def run_gate(args: argparse.Namespace, sandbox: Path, store: LessonStore) -> GateResult:
    result = GateResult([], [], [], [], [], [])
    document, status = load_playbook(Path(args.playbook))
    if document is None:
        print(f"{args.playbook} did not load ({status}); nothing to measure against", file=sys.stderr)
        return result
    holdout = holdout_cases(args.questions)

    for record in store.untested():
        if apply_inherited(store, record):
            result.inherited.append(record.lesson_id)
            continue
        case_id = source_case(record)
        if case_id is None or case_id in holdout:
            # Nothing to measure against, or measuring there would train on the holdout. Either way
            # the lesson stays untested and therefore unserved, which is the correct outcome.
            result.unmeasurable.append(record.lesson_id)
            continue
        print(f"  attributing {record.lesson_id} on {case_id}: {record.lesson[:70]}", flush=True)
        report = args.attribution_dir / f"{record.lesson_id}.json"
        completed = _run_attribution(args, record.lesson_id, case_id, report)
        if completed is None:
            result.unmeasurable.append(record.lesson_id)
            continue
        import_measured(store, record.lesson_id, json.loads(report.read_text(encoding="utf-8")), args.min_reps)
        result.measured.append(record.lesson_id)

    for lesson_id, verdict in (
        (r.lesson_id, r.attribution) for r in store.by_status("pending") if r.attribution != "untested"
    ):
        if verdict == "fixes":
            result.promoted.append(lesson_id)
        else:
            result.rejected.append((lesson_id, verdict))
            if verdict == "regresses":
                store.set_status(lesson_id, "quarantined")
                result.quarantined.append(lesson_id)
    return result


def _run_attribution(args: argparse.Namespace, lesson_id: str, case_id: str, report: Path) -> Path | None:
    """Drive eval/attrib.py as a subprocess, the same way the harness drives the usage batch."""
    import subprocess

    document, _ = load_playbook(Path(args.playbook))
    if document is None:
        return None
    stripped, _, _ = without_lesson(document, lesson_id)
    workdir = args.attribution_dir / lesson_id
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "playbook-without.json").write_text(stripped.to_json(), encoding="utf-8")

    command = [
        sys.executable,
        str(ROOT / "eval" / "attrib.py"),
        "--lesson-id",
        lesson_id,
        "--case",
        case_id,
        "--playbook",
        str(workdir / "playbook-without.json"),
        "--questions",
        args.questions,
        "--reps",
        str(args.reps),
        "--max-turns",
        str(args.max_turns),
        "--llm-url",
        args.llm_url,
        "--workdir",
        str(workdir),
        "--output",
        str(report),
    ]
    if args.no_thinking:
        command += ["--no-thinking"]
    if args.model:
        command += ["--model", args.model]
    completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True)
    if completed.returncode == 2:  # inconclusive: a real result, not a failure
        pass
    elif completed.returncode != 0:
        print(completed.stdout[-1500:], file=sys.stderr)
        print(completed.stderr[-1500:], file=sys.stderr)
        return None
    return report if report.is_file() else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", default=str(ROOT / "eval" / "harness"))
    parser.add_argument("--playbook", default=str(ROOT / "eval" / "harness" / "cache" / "playbook.json"))
    parser.add_argument("--questions", default=str(ROOT / "eval" / "generated" / "questions.json"))
    parser.add_argument("--attribution-dir", default=str(ROOT / "eval" / "harness" / "attribution"))
    parser.add_argument("--reps", type=int, default=MEANINGFUL_REPS)
    parser.add_argument("--min-reps", type=int, default=MEANINGFUL_REPS)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--llm-url", default="http://192.168.10.222:8081")
    parser.add_argument("--model")
    parser.add_argument("--no-thinking", action="store_true", help="match the agent the suite runs with")
    args = parser.parse_args()

    store = LessonStore(Path(args.sandbox) / "cache" / "lessons")
    result = run_gate(args, Path(args.sandbox), store)
    eligible, total = promote_candidate(Path(args.sandbox))
    print(json.dumps({**result.as_json(), "served_lessons": eligible, "document_lessons": total}, indent=2))


if __name__ == "__main__":
    main()
