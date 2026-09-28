"""Objective tool-call traces plus the pending lesson store.

Two things are recorded here. The first is objective and automatic: what tools were
called, whether they failed, and whether results were truncated. The second is
authored by the calling LLM through `benthic_report` and is always stored as `pending`.

Objective traces are written by the server. Model-authored lessons are written by the
report tool. Nothing in this module decides what is served; that is the consolidator
and the A/B gate. Question text is hashed rather than stored unless explicitly enabled.
"""

import asyncio
import hashlib
import json
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from benthic_mcp.playbook import LessonRecord, Playbook, similar


def _now() -> datetime:
    return datetime.now(UTC)


def _question_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class TraceEntry:
    tool: str
    ok: bool
    error_class: str | None = None
    error: str | None = None
    latency_ms: int = 0
    row_count: int | None = None
    truncated: bool | None = None
    source_complete: bool | None = None
    warning_count: int = 0
    arg_keys: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    at: datetime = field(default_factory=_now)

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "error_class": self.error_class,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "source_complete": self.source_complete,
            "warning_count": self.warning_count,
            "arg_keys": self.arg_keys,
            "sources": self.sources,
            "at": self.at.isoformat(),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "TraceEntry":
        return cls(
            tool=str(payload.get("tool", "unknown")),
            ok=bool(payload.get("ok")),
            error_class=payload.get("error_class"),
            error=payload.get("error"),
            latency_ms=int(payload.get("latency_ms") or 0),
            row_count=payload.get("row_count"),
            truncated=payload.get("truncated"),
            source_complete=payload.get("source_complete"),
            warning_count=int(payload.get("warning_count") or 0),
            arg_keys=list(payload.get("arg_keys") or []),
            sources=list(payload.get("sources") or []),
            at=datetime.fromisoformat(payload["at"]) if payload.get("at") else _now(),
        )


@dataclass(slots=True)
class StruggleSignature:
    """One objectively detected sign that a session went wrong.

    Only signals the server can actually observe. A confidently wrong answer with clean tool
    calls produces nothing here, which is why the holdout tripwire exists as a backstop.
    """

    kind: str
    tool: str
    detail: str
    source: str | None = None
    severity: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tool": self.tool,
            "detail": self.detail[:300],
            "source": self.source,
            "severity": self.severity,
        }


_UNSIGNED_JOIN = "not a signed bdp join path"
_UNKNOWN_COLUMN = "unknown columns"
_SEVERITY = {
    "unknown_column": 4,
    "join_rejected": 4,
    "max_turns": 3,
    "retry_loop": 3,
    "repeated_error": 2,
    "incomplete_source": 3,
    "empty_then_retried": 3,
    "tool_error": 1,
}


def _source_from_error(error: str | None) -> str | None:
    """Pull a qualified relation out of an error message so the lesson can be scoped."""
    if not error:
        return None
    match = re.search(r"\b([a-z][a-z0-9_]{1,30})\.([a-z][a-z0-9_]{1,60})\b", error.lower())
    return f"{match.group(1)}.{match.group(2)}" if match else None


def struggle_signatures(entries: list[TraceEntry], max_turns_hit: bool = False) -> list[StruggleSignature]:
    """Detect sessions that went wrong, from the trace log alone."""
    signatures: list[StruggleSignature] = []
    seen_errors: dict[tuple[str, str], int] = {}

    for index, entry in enumerate(entries):
        if not entry.ok and entry.error:
            message = entry.error.lower()
            key = (entry.tool, message[:80])
            seen_errors[key] = seen_errors.get(key, 0) + 1
            if _UNKNOWN_COLUMN in message:
                kind = "unknown_column"
            elif entry.tool == "join" and _UNSIGNED_JOIN in message:
                kind = "join_rejected"
            else:
                kind = "tool_error"
            signatures.append(
                StruggleSignature(
                    kind=kind,
                    tool=entry.tool,
                    detail=entry.error,
                    source=(entry.sources[0] if entry.sources else _source_from_error(entry.error)),
                    severity=_SEVERITY[kind],
                )
            )
        elif entry.ok and entry.source_complete is False:
            # `truncated` on its own means only that the agent asked for fewer rows than exist, which
            # is routine. `source_complete is False` is the real hazard: the underlying scan was
            # bounded, so any total derived from it is wrong.
            signatures.append(
                StruggleSignature(
                    kind="incomplete_source",
                    tool=entry.tool,
                    detail="the source scan was not complete, so counts or totals from it are unreliable",
                    source=entry.sources[0] if entry.sources else None,
                    severity=_SEVERITY["incomplete_source"],
                )
            )
        elif entry.ok and entry.row_count == 0:
            # An empty result is only a struggle when the agent reacted to it. On its own it is
            # often the correct answer, and flagging it swamps the real signal.
            followed_by_retry = any(later.ok and later.tool == entry.tool for later in entries[index + 1 :])
            if followed_by_retry:
                signatures.append(
                    StruggleSignature(
                        kind="empty_then_retried",
                        tool=entry.tool,
                        detail="a query returned no rows and the agent immediately tried again",
                        source=entry.sources[0] if entry.sources else None,
                        severity=_SEVERITY["empty_then_retried"],
                    )
                )

    for (tool, message), count in seen_errors.items():
        if count > 1:
            signatures.append(
                StruggleSignature(
                    kind="repeated_error" if count < 3 else "retry_loop",
                    tool=tool,
                    detail=f"the same error occurred {count} times: {message}",
                    severity=_SEVERITY["retry_loop"] if count >= 3 else _SEVERITY["repeated_error"],
                )
            )

    if max_turns_hit:
        signatures.append(
            StruggleSignature(
                kind="max_turns",
                tool="session",
                detail="the run ended without a final answer, so the requested work was not delivered",
                severity=_SEVERITY["max_turns"],
            )
        )
    return signatures


@dataclass(slots=True)
class LessonRecordView:
    record: LessonRecord
    similar_pending: int


class TraceStore:
    """Ring buffer for live context plus an append-only JSONL log.

    The ring buffer is process-local and advisory: it gives a report the recent tool
    history without requiring server-side session state, which `stateless_http=True`
    deliberately avoids.
    """

    def __init__(
        self,
        directory: Path,
        enabled: bool = True,
        retention_days: int = 30,
        include_text: bool = False,
        window: int = 50,
    ) -> None:
        self.directory = directory
        self.enabled = enabled
        self.retention_days = retention_days
        self.include_text = include_text
        self._recent: deque[TraceEntry] = deque(maxlen=window)

    @property
    def log_path(self) -> Path:
        return self.directory / "traces.jsonl"

    def record(self, entry: TraceEntry) -> None:
        self._recent.append(entry)
        if not self.enabled:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_json(), sort_keys=True) + "\n")
        except OSError:
            # Tracing is advisory and must never break a data request.
            return

    def recent(self, limit: int = 20) -> list[TraceEntry]:
        return list(self._recent)[-limit:]

    def recent_summary(self, limit: int = 20) -> list[str]:
        summary: list[str] = []
        for entry in self.recent(limit):
            parts = [entry.tool, "ok" if entry.ok else f"failed:{entry.error_class or 'unknown'}"]
            if entry.row_count is not None:
                parts.append(f"rows={entry.row_count}")
            if entry.truncated:
                parts.append("truncated")
            if entry.warning_count:
                parts.append(f"warnings={entry.warning_count}")
            summary.append(" ".join(parts))
        return summary

    def read_log(self, limit: int = 2000) -> list[dict[str, Any]]:
        if not self.log_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with self.log_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return rows[-limit:]

    def entries(self, limit: int = 2000) -> list[TraceEntry]:
        entries: list[TraceEntry] = []
        for row in self.read_log(limit):
            try:
                entries.append(TraceEntry.from_json(row))
            except (TypeError, ValueError):
                continue
        return entries

    def recurring_failures(self, limit: int = 20) -> list[tuple[str, int]]:
        """Error messages seen more than once, which is what the consolidator wants to learn from."""
        counts: dict[str, int] = {}
        for entry in self.entries():
            if entry.ok or not entry.error:
                continue
            counts[entry.error] = counts.get(entry.error, 0) + 1
        return sorted(((message, count) for message, count in counts.items() if count > 1), key=lambda item: -item[1])[
            :limit
        ]

    def sweep(self) -> int:
        if not self.enabled or not self.log_path.is_file():
            return 0
        cutoff = _now() - timedelta(days=self.retention_days)
        kept: list[str] = []
        dropped = 0
        try:
            with self.log_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if datetime.fromisoformat(entry["at"]) < cutoff:
                            dropped += 1
                            continue
                    except (ValueError, KeyError):
                        dropped += 1
                        continue
                    kept.append(line)
            if dropped:
                temporary = self.log_path.with_suffix(".jsonl.tmp")
                temporary.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                os.replace(temporary, self.log_path)
        except OSError:
            return 0
        return dropped


# Calibrated against the first observed round: identical paraphrases scored 0.37 and 0.71 by
# containment, unrelated pairs 0.16 to 0.25. 0.35 sits in the gap.
MERGE_THRESHOLD = 0.35
# Inheriting a measured verdict is a stronger claim than merging two records that agree, so it
# needs a stronger resemblance than the merge threshold.
INHERIT_THRESHOLD = 0.6
# Two lessons with identical text but different dataset scopes need a higher bar to merge.
CROSS_DATASET_MERGE_THRESHOLD = 0.45


class LessonStore:
    """One JSON file per lesson, so a regression can quarantine the offender alone."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, lesson_id: str) -> Path:
        return self.directory / f"{lesson_id}.json"

    def add(self, record: LessonRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(record.lesson_id).write_text(record.model_dump_json(indent=2), encoding="utf-8")

    def get(self, lesson_id: str) -> LessonRecord | None:
        path = self._path(lesson_id)
        if not path.is_file():
            return None
        try:
            return LessonRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, record: LessonRecord) -> None:
        self.add(record)

    def all(self) -> list[LessonRecord]:
        if not self.directory.is_dir():
            return []
        records: list[LessonRecord] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(LessonRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return records

    def by_status(self, status: str) -> list[LessonRecord]:
        return [record for record in self.all() if record.status == status]

    def similar(
        self, symptom: str, dataset: str | None, threshold: float = MERGE_THRESHOLD, include_active: bool = True
    ) -> list[LessonRecord]:
        matches = []
        for record in self.all():
            if record.status == "quarantined":
                continue
            if not include_active and record.status != "pending":
                continue
            if dataset is not None and record.dataset not in {None, dataset}:
                continue
            if similar(symptom, record.symptom) >= threshold:
                matches.append(record)
        return matches

    def similar_pending(
        self, symptom: str, dataset: str | None, threshold: float = MERGE_THRESHOLD
    ) -> list[LessonRecord]:
        return [
            record
            for record in self.similar(symptom, dataset, threshold, include_active=False)
            if record.status == "pending"
        ]

    def merge(self, record: LessonRecord, threshold: float = MERGE_THRESHOLD) -> LessonRecord:
        """Fold a repeat report into an existing lesson instead of storing a duplicate.

        Active lessons are included so reinforcement is cumulative. Two matching lessons that carry
        different dataset scopes are treated as evidence that the scope was incidental, so the
        survivor is widened to apply everywhere; that is what stops one identical correction being
        stored once per dataset. Cross-dataset matches need a higher bar than same-dataset ones.

        This scans the store directly rather than reusing `similar`, which is dataset-scoped because
        it feeds the "you already reported this" count shown to a reporting model.
        """
        for existing in self.all():
            if existing.status == "quarantined":
                continue
            score = similar(record.symptom, existing.symptom)
            if score < threshold:
                continue
            same_scope = existing.dataset == record.dataset or existing.dataset is None or record.dataset is None
            if not same_scope and score < CROSS_DATASET_MERGE_THRESHOLD:
                continue
            merged = existing.model_copy(
                update={
                    "occurrences": existing.occurrences + 1,
                    "last_seen": _now(),
                    "evidence": sorted({*existing.evidence, *record.evidence})[-12:],
                    "dataset": existing.dataset or record.dataset,
                }
            )
            if not same_scope:
                # The same correction attributed to two datasets is not dataset-specific.
                merged = merged.model_copy(update={"dataset": None})
            if merged.confidence == "low" and record.confidence == "medium":
                merged = merged.model_copy(update={"confidence": "medium"})
            self.save(merged)
            return merged
        self.add(record)
        return record

    def set_status(self, lesson_id: str, status: str) -> LessonRecord | None:
        record = self.get(lesson_id)
        if record is None:
            return None
        updated = record.model_copy(update={"status": status})
        self.save(updated)
        return updated

    def sweep(self, max_age_days: int) -> int:
        cutoff = _now() - timedelta(days=max_age_days)
        removed = 0
        for record in self.all():
            if record.status == "active" or record.last_seen >= cutoff:
                continue
            path = self._path(record.lesson_id)
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def active_records(self) -> list[LessonRecord]:
        return [record for record in self.all() if record.status == "active"]

    def candidate_lessons(self, catalog_fingerprint: str) -> list[LessonRecord]:
        """Lessons eligible for accumulation: the pending ones that were measured to help.

        Eligibility used to be "pending and not stale", which is a correctness check. Grounding a
        true statement is not the same as it changing behaviour, and the difference is the whole
        point: a store of grounded advice that demonstrably does nothing is a store of noise that
        costs prompt tokens on every turn and makes the always-on slice worse.

        A lesson therefore has to be attributed against the case it came from before it can be
        served. `eval/attribute_pending.py` does the measuring; this only decides eligibility.
        """
        return [record for record in self.by_status("pending") if record.attribution == "fixes"]

    def untested(self) -> list[LessonRecord]:
        """Lessons that have never been measured against a case, whatever their status.

        Deliberately not scoped to pending. A lesson promoted under the old rules - where
        eligibility meant only "grounded and not stale" - is active with no evidence behind it, and
        scoping this to pending would let those through untouched forever.
        """
        return [record for record in self.all() if record.attribution == "untested"]

    def set_attribution(
        self,
        lesson_id: str,
        verdict: str,
        *,
        case_id: str | None = None,
        reps: int = 0,
        inherited_from: str | None = None,
    ) -> LessonRecord | None:
        """Record what measuring the lesson did, so promotion can require evidence."""
        record = self.get(lesson_id)
        if record is None:
            return None
        updated = record.model_copy(
            update={
                "attribution": verdict,
                "attribution_case": case_id,
                "attribution_reps": reps,
                "attribution_inherited_from": inherited_from,
                "attributed_at": datetime.now(UTC),
            }
        )
        self.save(updated)
        return updated

    def inheritable(self, record: LessonRecord, threshold: float = INHERIT_THRESHOLD) -> LessonRecord | None:
        """A measured lesson that already answers the question this one raises.

        The reflector paraphrases, so the same advice arrives as a new record each round. Measuring
        each copy would burn an A/B per round per lesson forever. A near-duplicate of something
        already measured inherits its verdict, and the inheritance is recorded so the evidence is
        traceable to the lesson that actually gathered it.

        The bar is higher than the merge threshold on purpose. Merging two records that say the same
        thing is safe; handing one lesson a "fixes" verdict gathered by another is a stronger claim,
        and containment similarity will happily match two rules that share a function word.
        """
        for other in self.all():
            if other.lesson_id == record.lesson_id or other.attribution == "untested":
                continue
            if max(similar(record.lesson, other.lesson), similar(record.symptom, other.symptom)) >= threshold:
                return other
        return None

    def enforce_cap(self, cap: int) -> list[str]:
        """Trim the active set to `cap`, evicting the least reinforced first. Returns evicted ids."""
        active = sorted(self.by_status("active"), key=lambda item: (-item.occurrences, item.last_seen))
        evicted = [record.lesson_id for record in active[cap:]]
        for lesson_id in evicted:
            self.set_status(lesson_id, "evicted")
        return evicted


def merge_into_playbook(playbook: Playbook, lessons: list[LessonRecord]) -> Playbook:
    known = {record.lesson_id for record in playbook.lessons}
    additions = [record for record in lessons if record.lesson_id not in known]
    return playbook.model_copy(update={"lessons": [*playbook.lessons, *additions]})


async def sweep_all(store: TraceStore, lessons: LessonStore, retention_days: int) -> tuple[int, int]:
    traces, records = await asyncio.to_thread(store.sweep), await asyncio.to_thread(lessons.sweep, retention_days)
    return traces, records
