"""Consolidate pending lessons and observed failures into a candidate playbook.

The consolidator is deliberately not the source of truth. It reads the signed catalog, the
pending lesson store, the objective trace log, and recent evaluation failures, then writes a
candidate document. Nothing it writes is served: `eval/promote.py` must first show that the
candidate beats the current playbook on the tuning cases without regressing the holdout cases.

The optional LLM pass only ever rephrases and compresses lessons that already exist. It is
not asked to discover facts about the catalog, and every sentence it returns is screened by
`benthic_mcp.playbook.verify` before being accepted.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from benthic_mcp.bdp import BdpRepository  # noqa: E402
from benthic_mcp.catalog import Catalog  # noqa: E402
from benthic_mcp.config import Settings  # noqa: E402
from benthic_mcp.playbook import (  # noqa: E402
    LessonRecord,
    Playbook,
    VerifyReport,
    known_identifiers,
    screen_prose,
    similar,
    verify,
)
from benthic_mcp.seed import seed_playbook  # noqa: E402
from benthic_mcp.trace import LessonStore, TraceStore  # noqa: E402

SYSTEM_PROMPT = """You compress analyst lessons into short access guidance for a read-only data MCP.

Hard rules, in order of priority:
1. Never name a dataset, relation, or column that is not listed in the INPUT CATALOG.
2. Never suggest a join that is not in the SIGNED JOIN PATHS list.
3. Never invent a fact about the data. You have no samples; describe conventions, not values.
4. Each output line must be one short imperative sentence a future assistant can act on.
5. If a lesson is unclear, contradictory, or dataset-specific trivia, omit it. Omission is free.

Return only JSON of the form {"core": [string], "anti_patterns": {"<dataset>": [string]}}.

Answer immediately with the JSON object. Do not deliberate and do not restate the question: this model
tends to spend its whole token budget reasoning and then emit no content at all, which silently
disables the core rules and with them the holdout tripwire."""


def resolve_model(base_url: str) -> str:
    response = httpx.get(f"{base_url.rstrip('/')}/v1/models", timeout=30.0)
    response.raise_for_status()
    models = response.json().get("data") or []
    if not models:
        raise SystemExit("The LLM returned no model")
    return str(models[0]["id"])


async def chat(base_url: str, model: str, system: str, user: str, timeout: float) -> tuple[str, str]:
    """Returns (content, finish_reason). Reasoning models can spend the whole budget thinking."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 4000,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{base_url.rstrip('/')}/v1/chat/completions", json=payload)
        response.raise_for_status()
    choices = response.json().get("choices") or []
    if not choices:
        return "", "no_choices"
    message = choices[0].get("message", {})
    content = str(message.get("content") or "").strip()
    if not content:
        # Some builds only emit the answer after the reasoning block; if the budget ran out
        # mid-thought there is nothing to salvage, which the caller reports.
        content = str(message.get("reasoning_content") or "").strip()
        if content:
            return "", "reasoning_only"
    return content, str(choices[0].get("finish_reason") or "unknown")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(raw: str) -> dict[str, Any] | None:
    """Models often wrap JSON in a markdown fence or add a sentence around it."""
    candidates = [raw, *(_FENCE.findall(raw) or [])]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def read_eval_failures(runs_dir: Path, limit: int) -> list[dict[str, Any]]:
    if not runs_dir.is_dir():
        return []
    failures: list[dict[str, Any]] = []
    for run in sorted(runs_dir.iterdir(), reverse=True)[:limit]:
        path = run / "failures.json"
        if not path.is_file():
            continue
        try:
            for record in json.loads(path.read_text(encoding="utf-8")):
                failures.append({"run": run.name, **record})
        except ValueError:
            continue
    return failures


def dedupe(lessons: list[LessonRecord], threshold: float = 0.4) -> list[LessonRecord]:
    kept: list[LessonRecord] = []
    for lesson in sorted(lessons, key=lambda item: -item.occurrences):
        for existing in kept:
            if existing.dataset == lesson.dataset and similar(existing.symptom, lesson.symptom) >= threshold:
                kept[kept.index(existing)] = existing.model_copy(
                    update={"occurrences": existing.occurrences + lesson.occurrences}
                )
                break
        else:
            kept.append(lesson)
    return kept


def carry_forward(baseline_lessons: list[LessonRecord], store: Any) -> tuple[list[LessonRecord], list[str]]:
    """Split the current document's lessons into those with evidence and those to re-measure.

    A lesson promoted under the old rules - where eligibility meant grounded and not stale, which
    says nothing about usefulness - has to be demoted to pending here rather than inherited by the
    next document, or the document is self-perpetuating: everything in it was placed there by a round
    that no longer applies, and no measurement can ever reach it.
    """
    carried: list[LessonRecord] = []
    demoted: list[str] = []
    for record in baseline_lessons:
        if record.attribution == "fixes":
            carried.append(record)
            continue
        demoted.append(record.lesson_id)
        store.set_status(record.lesson_id, "pending")
    return carried, demoted


def diff_markdown(baseline: Playbook, candidate: Playbook) -> str:
    lines = ["# Playbook candidate", ""]
    for label, before, after in (
        ("core", baseline.core, candidate.core),
        (
            "lessons",
            [f"{item.symptom} -> {item.lesson}" for item in baseline.lessons],
            [f"{item.symptom} -> {item.lesson}" for item in candidate.lessons],
        ),
    ):
        removed = [item for item in before if item not in after]
        added = [item for item in after if item not in before]
        lines.append(f"## {label} ({len(added)} added, {len(removed)} removed)")
        for item in added:
            lines.append(f"- added: {item}")
        for item in removed:
            lines.append(f"- removed: {item}")
        lines.append("")
    for name in sorted(set(candidate.datasets) | set(baseline.datasets)):
        before_patterns = baseline.datasets[name].anti_patterns if name in baseline.datasets else []
        after_patterns = candidate.datasets[name].anti_patterns if name in candidate.datasets else []
        added = [item for item in after_patterns if item not in before_patterns]
        if added:
            lines.append(f"## anti_patterns for {name}")
            lines.extend(f"- added: {item}" for item in added)
            lines.append("")
    return "\n".join(lines) + "\n"


async def consolidate(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    paths = {
        "active": args.playbook or (settings.cache_dir / "playbook.json"),
        "candidate": args.output,
        "lessons": settings.cache_dir / "lessons",
    }

    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, follow_redirects=False) as client:
        catalog = Catalog(await BdpRepository(settings, client).load())

    if paths["active"].is_file():
        baseline = Playbook.from_json(paths["active"].read_text(encoding="utf-8"))
    else:
        baseline = seed_playbook()

    lessons = LessonStore(paths["lessons"])
    traces = TraceStore(settings.cache_dir / "traces", enabled=True)

    # Carry forward only what still has evidence behind it. A lesson promoted under the old rules -
    # where eligibility meant grounded and not stale, which says nothing about usefulness - is
    # demoted to pending here rather than inherited by the new document, so the gate gets to measure
    # it. Without this the document is self-perpetuating: everything in it was put there by a round
    # that no longer applies.
    carried, demoted = carry_forward(baseline.lessons, lessons)
    promoted = [
        record.model_copy(update={"status": "active", "catalog_fingerprint": catalog.fingerprint()})
        for record in lessons.candidate_lessons(catalog.fingerprint())
    ]
    merged = dedupe([*carried, *promoted])
    candidate = baseline.model_copy(
        update={
            "catalog_fingerprint": catalog.fingerprint(),
            "generated_at": datetime.now(UTC),
            "generator": f"consolidate.py from {len(promoted)} pending lessons",
            "lessons": merged,
            "stats": {
                "pending_promoted": len(promoted),
                "lessons_carried_with_evidence": len(carried),
                "lessons_demoted_for_measurement": len(demoted),
                "lessons_total": len(merged),
                "quarantined": len(lessons.by_status("quarantined")),
            },
        }
    )

    failures = read_eval_failures(args.runs_dir, args.failure_runs)
    note = "skipped"
    core_changed = False
    if not args.no_llm and (merged or failures):
        note, core, datasets, before = await llm_pass(args, catalog, merged, failures, candidate)
        core_changed = core != before
        candidate = candidate.model_copy(update={"core": core, "datasets": datasets})

    verified, report = verify(candidate, catalog)
    verified = verified.model_copy(update={"generator": f"{candidate.generator}; llm={note}"})
    verified, pruned = enforce_lesson_cap(verified, args.lesson_cap)

    args.output.write_text(verified.to_json(), encoding="utf-8")
    args.output.with_suffix(".md").write_text(diff_markdown(baseline, verified), encoding="utf-8")

    summary = {
        "candidate": str(args.output),
        "fingerprint": catalog.fingerprint(),
        "promoted_lessons": len(promoted),
        "lessons_total": len(verified.lessons),
        "lessons_pruned_by_cap": pruned,
        "core_changed": core_changed,
        "eval_failures_seen": len(failures),
        "recurring_failures": traces.recurring_failures()[:5],
        "verification_drops": report.as_dict(),
        "llm": note,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


def enforce_lesson_cap(playbook: Playbook, cap: int) -> tuple[Playbook, int]:
    """Keep the served playbook from growing without bound.

    Lessons are the cheap half of the playbook and accumulate, so without a cap the per-turn cost
    of `benthic_playbook` grows monotonically. The least reinforced go first.
    """
    if cap <= 0 or len(playbook.lessons) <= cap:
        return playbook, 0
    ranked = sorted(playbook.lessons, key=lambda item: (-item.occurrences, item.confidence))
    kept = sorted(ranked[:cap], key=lambda item: item.lesson_id)
    return playbook.model_copy(update={"lessons": kept}), len(ranked) - cap


async def llm_pass(
    args: argparse.Namespace,
    catalog: Catalog,
    lessons: list[LessonRecord],
    failures: list[dict[str, Any]],
    baseline: Playbook,
) -> tuple[str, list[str], dict[str, Any], list[str]]:
    """Ask the model to compress existing lessons. Returns (note, core, datasets, core_before)."""
    base_url = os.environ.get("BENTHIC_CONSOLIDATOR_LLM_URL", args.llm_url)
    core = list(baseline.core)
    datasets = {name: section.model_copy(deep=True) for name, section in baseline.datasets.items()}
    core_before = list(baseline.core)

    try:
        model = os.environ.get("BENTHIC_CONSOLIDATOR_MODEL", "") or resolve_model(base_url)
    except (httpx.HTTPError, SystemExit) as exc:
        return f"unavailable ({type(exc).__name__})", core, datasets, core_before

    dataset_names = sorted({record.dataset for record in lessons if record.dataset} | set(catalog.datasets))
    catalog_digest = {
        name: {
            "title": catalog.datasets[name].title,
            "relations": sorted(relation for dataset_name, relation in catalog.relations if dataset_name == name),
        }
        for name in dataset_names
        if name in catalog.datasets
    }
    join_paths = [
        f"{join.from_dataset}.{join.from_relation}.{join.from_column} -> "
        f"{join.to_dataset}.{join.to_relation}.{join.to_column} ({join.reliability.value})"
        for join in catalog.joins
    ]

    lesson_lines = [
        f"- [{record.dataset or 'any'}{'/' + record.relation if record.relation else ''}] "
        f"{record.symptom} => {record.lesson} (x{record.occurrences})"
        for record in lessons
    ] or ["- none"]
    failure_lines = [f"- {failure.get('id')}: {json.dumps(failure.get('score', {}))}" for failure in failures[:20]] or [
        "- none"
    ]
    join_lines = [f"- {path}" for path in join_paths] or ["- none"]

    user = "\n".join(
        [
            "INPUT CATALOG:",
            json.dumps(catalog_digest, indent=1),
            "",
            "SIGNED JOIN PATHS:",
            *join_lines,
            "",
            "LESSONS REPORTED BY CALLING MODELS:",
            *lesson_lines,
            "",
            "EVALUATION FAILURES:",
            *failure_lines,
        ]
    )

    try:
        raw, finish = await chat(base_url, model, SYSTEM_PROMPT, user, args.request_timeout)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return f"unavailable ({type(exc).__name__})", core, datasets, core_before
    if not raw:
        return f"no content (finish={finish})", core, datasets, core_before
    if finish == "length":
        return f"truncated at max_tokens, skipped (finish={finish})", core, datasets, core_before

    payload = extract_json(raw)
    if payload is None:
        return f"unparsable response (finish={finish}, {len(raw)} chars)", core, datasets, core_before

    report = VerifyReport()
    known = known_identifiers(catalog)
    applied = 0
    for line in list(payload.get("core") or [])[:8]:
        for sentence in screen_prose(str(line), catalog, known, report):
            if sentence not in core:
                core.append(sentence)
                applied += 1
    for name, patterns in (payload.get("anti_patterns") or {}).items():
        if name not in datasets or not isinstance(patterns, list):
            continue
        for line in patterns[:6]:
            for sentence in screen_prose(str(line), catalog, known, report):
                if sentence not in datasets[name].anti_patterns:
                    datasets[name].anti_patterns.append(sentence)
                    applied += 1

    return f"applied {applied} lines, dropped {report.total_dropped}", core[:8], datasets, core_before


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playbook", type=Path, default=None, help="current active playbook, if any")
    parser.add_argument("--output", type=Path, default=None, help="candidate path")
    parser.add_argument("--runs-dir", type=Path, default=Path("eval/runs"))
    parser.add_argument("--failure-runs", type=int, default=10)
    parser.add_argument("--llm-url", default="http://192.168.10.222:8081")
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--lesson-cap", type=int, default=40, help="max active lessons; 0 disables")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.output is None:
        args.output = settings.cache_dir / "playbook-candidate.json"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(consolidate(args)))


if __name__ == "__main__":
    main()
