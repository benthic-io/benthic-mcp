"""Which always-on core lines have been measured worth their tokens, and where that is recorded.

A core line is re-sent on every turn, so it has to earn its place every turn. Per-case attribution
cannot check that for a general rule: the answer-delivery rule measured 0/3 -> 0/3 on the case it was
pointed at, and is worth two cases across the suite, because it helps five cases by one each and
nothing in particular on any one of them. `eval/attribute_suite.py` is the instrument that can see
that, and this is where its verdict is kept so the consolidator can put a passing rule into the core
instead of re-deriving it from lessons that measured no effect.

Two rules, and they are the whole point of the file existing:

Only a `fixes` verdict is servable. An absent, unreadable or negative verdict is a rule that was
measured and did not earn its place, and it stays out of the context. `read` returns the lines that
may be served; `record` is what the measurement writes.

A record is not a promotion. It says a rule helped once, on one split, at one repetition count, and it
keeps the run that established it so the claim can be re-checked rather than believed. Nothing here
decides that a rule should exist.
"""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# The only verdict that puts a line into the always-on context. Everything else measured the rule and
# found it did not earn the tokens it would cost on every turn.
SERVABLE = "fixes"


class CoreEvidence(BaseModel):
    """One measured claim about one rule."""

    text: str
    verdict: str
    run: str = ""
    split: str = ""
    reps: int = 0
    delta: int = 0
    baseline: str = ""
    candidate: str = ""
    measured_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    @property
    def servable(self) -> bool:
        return self.verdict == SERVABLE

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def load(path: Path) -> list[CoreEvidence]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [CoreEvidence.model_validate(item) for item in payload]
    except ValueError:
        # A corrupt file must not be read as an empty one, because empty means "nothing has been
        # measured" and that would quietly drop every rule the file was protecting.
        raise SystemExit(f"{path} is not readable as core evidence; refusing to treat it as empty") from None


def save(path: Path, records: list[CoreEvidence]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = sorted((record.as_json() for record in records), key=lambda item: item["text"])
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def servable_lines(records: list[CoreEvidence]) -> list[str]:
    """The measured lines that may join the always-on core, in a stable order."""
    return sorted({record.text.strip() for record in records if record.servable and record.text.strip()})


def from_report(report: dict[str, Any]) -> CoreEvidence:
    """Read an eval/attribute_suite.py report into a record.

    A report whose verdict is not `fixes` is still recorded. A rule measured as useless is worth
    remembering precisely so it is not proposed again, and dropping the negative verdicts would lose
    that.
    """
    return CoreEvidence(
        text=str(report["rule"]).strip(),
        verdict=str(report["verdict"]).split(":")[0],
        run=str(report.get("candidate", {}).get("playbook", "")),
        split="tuning",
        reps=int(report.get("reps", 0)),
        delta=int(report.get("delta", 0)),
        baseline=f"{report.get('baseline', {}).get('passed')}/{report.get('baseline', {}).get('cases')}",
        candidate=f"{report.get('candidate', {}).get('passed')}/{report.get('candidate', {}).get('cases')}",
    )


def record(path: Path, report_path: Path) -> CoreEvidence:
    """Add or replace the record for a rule, keeping one entry per rule.

    One entry per rule rather than per run, because the file is a claim about the rule, not a log. The
    latest measurement replaces the previous one, which is what stops a rule that later measured as
    useless from surviving on the strength of an earlier pass.
    """
    report = json.loads(report_path.read_text(encoding="utf-8"))
    entry = from_report(report)
    others = [item for item in load(path) if item.text != entry.text]
    save(path, [*others, entry])
    return entry
