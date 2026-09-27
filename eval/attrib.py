"""Does one lesson actually fix the failure it was written from?

The end-to-end pass rate cannot answer that. Thirty-three cases at 84% against a stochastic model
moves by one or two cases between rounds for no reason, which is how six rounds of accumulation
produced a flat line that was really two single-rep coin flips. A single-case A/B has a usable
signal-to-noise ratio, so this measures the thing a lesson is supposed to do: remove the lesson
from the served playbook, run the case it came from with and without it, and report the paired
counts.

Two things make the result mean something:

- The "without" arm is a real removal, not an omission. The lesson is dropped from the document and
  any core line it was distilled into is dropped with it, because a core line that still carries the
  advice makes both arms carry the lesson and the comparison measures nothing.
- A sibling case with a different surface question and the same capability runs alongside. Without
  it, a lesson that simply restates the answer scores as a fix.

The per-case counts are the real output. The verdict is a convenience label over them and is only
meaningful when the case is not itself flipping between repetitions.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "src"))

from benthic_mcp.playbook import LessonRecord, Playbook, content_words, load_playbook  # noqa: E402

# A core line this similar to a lesson is treated as a distillation of it and removed with it.
# Calibrated on the served playbook: a line genuinely distilled from a lesson scores 0.45-1.0, the
# nearest line that is a different rule scores 0.16, and everything unrelated scores 0.0.
CORE_OVERLAP_THRESHOLD = 0.4
# Two shared content words is what "answer a historical question" and "deliver a final answer" have
# in common, so a floor is needed as well as a ratio.
CORE_MIN_SHARED_WORDS = 3


def core_similarity(lesson_text: str, core_line: str) -> float:
    """How much of a core line is accounted for by a lesson, as Jaccard over content words.

    Not the containment measure the lesson store uses. Containment asks whether the shorter text is
    inside the longer one, which suits a lesson and its long paraphrase, but a core line and the
    lesson it came from are two short restatements of the same rule, so the right question is how
    much they agree, not whether one contains the other. Containment scored the unrelated
    current-only-view rule as a distillation of the answer-delivery lesson on the shared word
    "answer", which would have dropped a legitimate core line and quietly corrupted the arm.
    """
    left, right = content_words(lesson_text), content_words(core_line)
    if not left or not right:
        return 0.0
    shared = left & right
    if len(shared) < CORE_MIN_SHARED_WORDS:
        return 0.0
    return len(shared) / len(left | right)


def core_lines_from(lessons: list[LessonRecord], core: list[str]) -> list[str]:
    """Core lines the consolidator plausibly distilled out of any of these lessons."""
    return [
        line
        for line in core
        if any(
            max(core_similarity(record.lesson, line), core_similarity(record.symptom, line)) >= CORE_OVERLAP_THRESHOLD
            for record in lessons
        )
    ]


def removal_set(document: Playbook, lesson: LessonRecord) -> tuple[set[str], list[str]]:
    """The lessons and core lines to drop so this advice is genuinely absent from the served playbook.

    Redundancy is the normal state of an accumulated playbook: the served core carries three
    separate lines about unknown columns, contributed by lessons that are not lexically similar to
    each other at all. Removing the one lesson being measured therefore leaves the model told the
    same thing by its neighbours, and the "without" arm still contains the advice, so the
    comparison reports no effect no matter what the lesson is worth.

    So the unit that can be tested is the advice, not the record that stated it first. The set is
    computed as a fixed point over the core: removing a lesson drops the core lines it produced,
    and any lesson that produced one of those lines joins the set, which can drop more lines still.
    """
    lesson_ids = {lesson.lesson_id}
    lines: list[str] = []
    changed = True
    while changed:
        changed = False
        for line in core_lines_from([r for r in document.lessons if r.lesson_id in lesson_ids], document.core):
            if line not in lines:
                lines.append(line)
                changed = True
        for record in document.lessons:
            if record.lesson_id in lesson_ids:
                continue
            if set(core_lines_from([record], document.core)) & set(lines):
                lesson_ids.add(record.lesson_id)
                changed = True
    return lesson_ids, lines


def without_lesson(document: Playbook, lesson_id: str, cohort: bool = True) -> tuple[Playbook, list[str], list[str]]:
    """The served playbook with the lesson's advice genuinely absent, core lines included."""
    matches = [record for record in document.lessons if record.lesson_id == lesson_id]
    if not matches:
        raise SystemExit(f"no lesson {lesson_id} in the playbook ({len(document.lessons)} lessons present)")
    lesson = matches[0]
    if cohort:
        removing, removed = removal_set(document, lesson)
    else:
        removing = {lesson_id}
        removed = core_lines_from([lesson], document.core)
    also = sorted(record.lesson_id for record in document.lessons if record.lesson_id in removing - {lesson_id})
    stripped = document.model_copy(deep=True)
    stripped.lessons = [record for record in stripped.lessons if record.lesson_id not in removing]
    stripped.core = [line for line in stripped.core if line not in removed]
    return stripped, removed, also


def flaky(outcomes: list[bool]) -> bool:
    return 0 < sum(outcomes) < len(outcomes)


def judge(without: list[bool], with_lesson: list[bool], min_delta: int) -> dict[str, Any]:
    """Label a paired comparison, and say plainly when it cannot be read.

    Instability only makes a result unreadable when the effect is small. A lesson that takes a case
    from 0/5 to 3/5 produces mixed outcomes in the arm that works, and calling that inconclusive
    would throw away the partial effects this exists to find. A one-rep difference on a case that
    already flips on its own is the opposite case, and that is the one the guard mistook for a
    regression.
    """
    lost, gained = sum(without), sum(with_lesson)
    delta = gained - lost
    unstable = flaky(without) or flaky(with_lesson)
    if delta >= min_delta:
        verdict = "fixes"
    elif delta <= -min_delta:
        verdict = "regresses"
    else:
        verdict = "inconclusive" if unstable else "no_effect"
    return {
        "without": f"{lost}/{len(without)}",
        "with": f"{gained}/{len(with_lesson)}",
        "delta": delta,
        "verdict": verdict,
        "unstable": unstable,
        "confidence": confidence(delta, len(without), min_delta),
    }


def confidence(delta: int, reps: int, min_delta: int) -> str:
    """How much a delta of this size is worth at this rep count, stated rather than implied."""
    if delta >= reps:
        return "strong"
    if abs(delta) < min_delta:
        return "none"
    return "suggestive"


def find_case(cases: list[dict[str, Any]], needle: str) -> dict[str, Any]:
    matches = [case for case in cases if needle in str(case["id"])]
    if not matches:
        raise SystemExit(f"no case matching {needle!r}")
    if len(matches) > 1:
        raise SystemExit(f"{needle!r} matches {len(matches)} cases: {[case['id'] for case in matches]}")
    return matches[0]


def run_once(args: argparse.Namespace, case_id: str, playbook: Path, outdir: Path) -> dict[str, Any]:
    """One case, one arm, one repetition, as its own process.

    A subprocess rather than a second in-process server because `in_process_mcp` installs the
    service as a module-level global (`run_eval._service`), so two concurrent arms would overwrite
    each other and both would serve the same playbook. That failure is silent and it reads as "this
    lesson has no effect", which is the one conclusion this tool must never produce by accident.
    """
    command = [
        sys.executable,
        str(ROOT / "eval" / "run_eval.py"),
        "--questions",
        args.questions,
        "--case-filter",
        case_id,
        "--reps",
        "1",
        "--strict",
        "--in-process",
        "--split",
        "all",
        "--playbook",
        str(playbook),
        "--llm-url",
        args.llm_url,
        "--max-turns",
        str(args.max_turns),
        "--max-tokens",
        str(args.max_tokens),
        "--temperature",
        str(args.temperature),
        "--top-p",
        str(args.top_p),
        "--top-k",
        str(args.top_k),
        "--request-timeout",
        str(args.request_timeout),
        "--output-dir",
        str(outdir),
    ]
    if args.model:
        command += ["--model", args.model]
    completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True)
    if completed.returncode != 0:
        print(completed.stdout[-2000:], file=sys.stderr)
        print(completed.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"{case_id} arm failed with exit code {completed.returncode}")
    run_dir = sorted(outdir.iterdir())[-1]
    records = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    if len(records) != 1:
        raise SystemExit(f"{case_id} produced {len(records)} results, expected 1")
    return records[0]


def measure(
    args: argparse.Namespace, case: dict[str, Any], arms: dict[str, Path], outroot: Path, reps: int
) -> dict[str, list[bool]]:
    """Run one case against both arms, alternating so server drift hits both equally.

    Interleaving at the finest granularity matters: llama-server batches continuously, so running
    every repetition of one arm and then every repetition of the other would attribute any
    drift over the course of the run to the lesson.
    """
    outcomes: dict[str, list[bool]] = {name: [] for name in arms}
    for rep in range(reps):
        for name, playbook in arms.items():
            started = time.perf_counter()
            outdir = outroot / name
            outdir.mkdir(parents=True, exist_ok=True)
            record = run_once(args, str(case["id"]), playbook, outdir)
            passed = bool(record["score"]["passed"])
            outcomes[name].append(passed)
            print(
                f"  {case['id']} rep {rep + 1}/{reps} {name}: {'pass' if passed else 'FAIL'}"
                f" ({time.perf_counter() - started:.0f}s)",
                flush=True,
            )
    return outcomes


def render(report: dict[str, Any]) -> str:
    lines = [
        f"# Attribution: {report['lesson_id']}",
        "",
        f"- lesson: {report['lesson_text']}",
        f"- source case: {report['case']}",
        f"- reps per arm: {report['reps']} (alternating)",
    ]
    if report["cohort_lessons_removed"]:
        lines.append(
            f"- {len(report['cohort_lessons_removed'])} near-duplicate lesson(s) removed with it,"
            " because a redundant playbook still carries the same advice through its neighbours"
            f" ({', '.join(report['cohort_lessons_removed'])})"
        )
    elif not report["cohort"]:
        lines.append(
            "- measured the lesson without its cohort (--no-cohort), so a redundant playbook may still carry the advice"
        )
    if report["dropped_core_lines"]:
        lines.append(
            f"- core lines removed with it: {len(report['dropped_core_lines'])}"
            " (the lesson was distilled into the always-on slice, so leaving them would have"
            " put the advice in both arms)"
        )
    else:
        lines.append("- the lesson is not present in the always-on core, so removal is clean")
    if report["sibling"]:
        lines.append(f"- sibling case: {report['sibling']}")
    else:
        lines.append("- sibling case: none, so a lesson that restates the answer could still score as a fix")
    lines += [
        "",
        "| case | without | with | delta | verdict | strength |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, outcome in report["results"].items():
        lines.append(
            f"| {name} | {outcome['without']} | {outcome['with']} | {outcome['delta']:+d} |"
            f" {outcome['verdict']} | {outcome['confidence']} |"
        )
    primary = report["results"][report["case"]]
    lines += [
        "",
        f"Verdict on the source case: **{primary['verdict']}** ({primary['confidence']}).",
    ]
    if primary["verdict"] == "inconclusive":
        lines.append(
            "The case flips between repetitions on its own and the two arms are within "
            f"{report['min_delta']} rep(s) of each other, so this comparison is not readable. "
            "Raise --reps before drawing a conclusion from it."
        )
    elif primary["confidence"] == "suggestive":
        lines.append(
            "The effect is real but partial: it does not hold on every repetition. Treat it as a "
            "screen, not a measurement, and require --min-delta equal to --reps before promoting on it."
        )
    if report["sibling"]:
        sibling = report["results"][report["sibling"]]
        if sibling["verdict"] == "regresses":
            lines.append(
                f"The sibling case got worse ({sibling['without']} -> {sibling['with']}). A lesson that "
                "helps its own case while hurting a paraphrase of it is memorising the answer."
            )
    lines += [
        "",
        "Per-case counts are the result. The verdict is a label over them and carries no weight when",
        "the row is marked noisy.",
    ]
    return "\n".join(lines) + "\n"


def attribute(args: argparse.Namespace) -> int:
    document, status = load_playbook(Path(args.playbook))
    if document is None:
        raise SystemExit(f"{args.playbook} did not load ({status})")
    stripped, dropped, also_dropped = without_lesson(document, args.lesson_id, cohort=not args.no_cohort)
    lesson = next(record for record in document.lessons if record.lesson_id == args.lesson_id)

    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))["cases"]
    source = find_case(questions, args.case)
    siblings = [source]
    if args.sibling:
        sibling = find_case(questions, args.sibling)
        if sibling["capability"] != source["capability"]:
            raise SystemExit(
                f"sibling {sibling['id']} is {sibling['capability']}, source is {source['capability']}: "
                "a sibling has to exercise the same capability or it tests nothing"
            )
        siblings.append(sibling)

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    with_path = workdir / "playbook-with.json"
    without_path = workdir / "playbook-without.json"
    with_path.write_text(document.to_json(), encoding="utf-8")
    without_path.write_text(stripped.to_json(), encoding="utf-8")

    print(
        f"lesson {args.lesson_id}: {len(document.lessons)} in the served playbook,"
        f" {len(stripped.lessons)} without it, {len(dropped)} core line(s) and"
        f" {len(also_dropped)} near-duplicate lesson(s) dropped with it",
        flush=True,
    )

    arms = {"with": with_path, "without": without_path}
    results: dict[str, Any] = {}
    for case in siblings:
        print(f"\n=== {case['id']} ({case['capability']}) ===", flush=True)
        outcomes = measure(args, case, arms, workdir / "runs", args.reps)
        results[case["id"]] = judge(outcomes["without"], outcomes["with"], args.min_delta)

    report = {
        "lesson_id": args.lesson_id,
        "lesson_text": lesson.lesson,
        "symptom": lesson.symptom,
        "case": source["id"],
        "sibling": siblings[1]["id"] if len(siblings) > 1 else None,
        "reps": args.reps,
        "min_delta": args.min_delta,
        "cohort": not args.no_cohort,
        "cohort_lessons_removed": also_dropped,
        "dropped_core_lines": dropped,
        "results": results,
        "lessons_served": len(document.lessons),
        "lessons_without": len(stripped.lessons),
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out.with_suffix(".md")).write_text(render(report), encoding="utf-8")
    print("\n" + render(report))
    print(f"Artifacts: {out}")

    primary = results[source["id"]]
    if primary["verdict"] == "inconclusive":
        return 2
    if primary["verdict"] == "regresses":
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lesson-id", required=True)
    parser.add_argument("--case", required=True, help="the case the lesson was extracted from")
    parser.add_argument("--sibling", help="a different question of the same capability, to catch answer restatement")
    parser.add_argument("--playbook", required=True, help="the served playbook to measure against")
    parser.add_argument("--questions", default=str(ROOT / "eval" / "generated" / "questions.json"))
    parser.add_argument(
        "--no-cohort",
        action="store_true",
        help=(
            "keep the near-duplicate lesson records and remove only this one. Core lines distilled "
            "from it are still dropped, since leaving those would put the advice back in the "
            "always-on slice and the arm would measure nothing."
        ),
    )
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument(
        "--min-delta",
        type=int,
        default=2,
        help="reps of improvement needed to call a lesson a fix; below this the counts are reported as no effect",
    )
    parser.add_argument("--llm-url", default="http://192.168.10.222:8081")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument("--workdir", default=str(ROOT / "eval" / "attrib"))
    parser.add_argument("--output", default=str(ROOT / "eval" / "attrib" / "report.json"))
    raise SystemExit(attribute(parser.parse_args()))


if __name__ == "__main__":
    main()
