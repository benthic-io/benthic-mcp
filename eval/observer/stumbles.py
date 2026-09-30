#!/usr/bin/env python3
"""Group observed model reasoning into recurring stumbles, and check each candidate against the
signed manifest before anything may become guidance.

The loop this feeds is: probe continuously, notice the model getting the same thing wrong, write the
answer into the standing guidance so it never has to work it out again. The risk in that loop is
believing the model. It has been wrong about the catalog more than once tonight - it reasoned that
toptier_code `020` was the Department of Veterans Affairs, when it is the Department of the
Treasury - so a candidate is never taken on the model's word. It is checked against the manifest,
and a candidate that does not check out is recorded as rejected with the reason.

Writes eval/observer/stumbles.json: the candidates worth writing into guidance, and the ones that
did not survive verification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# A model that guesses a column, or invents a relation name, or re-issues a call the server just
# rejected. Each pattern is (name, regex over the reasoning and the tool arguments).
STUMBLE_PATTERNS: list[tuple[str, str]] = [
    ("guessed_column", r"(?i)\b(?:no|does not|doesn't|is not)\s+(?:have|has)\s+(?:a\s+)?[`'\"]?(\w+)"),
    ("guessed_relation", r"(?i)\b(?:the\s+)?(\w+)\s+table\s+(?:has|presumably|probably)"),
    ("assumed_name_column", r"(?i)\bname\s+column\b"),
    ("retried_rejected_call", r"(?i)\b(?:let me try|retry|again|instead)\b"),
    ("reasoned_about_missing", r"(?i)\b(?:presumably|probably|presumably also|i think)\b"),
]


def load_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def blob(turn: dict[str, Any]) -> str:
    """Everything a turn said or tried. Reasoning first, because that is where a guess shows."""
    parts = [turn.get("reasoning") or "", turn.get("content") or ""]
    for call in turn.get("tool_calls", []):
        parts.append(call.get("arguments") or "")
    for result in turn.get("tool_results", []):
        # The error tail is what shows a guess was wrong; the head is usually the payload.
        parts.append(result.get("text", "")[:400])
    return " ".join(parts)


def stumbles(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        for turn in record.get("turns", []):
            text = blob(turn)
            for name, pattern in STUMBLE_PATTERNS:
                for match in re.findall(pattern, text):
                    found[name].append(
                        {
                            "probe": record.get("id"),
                            "turn": turn.get("turn"),
                            "match": match if isinstance(match, str) else match[0],
                        }
                    )
    return found


async def signed_catalog():
    """The real manifest, from the repository cache rather than the network."""
    import sys

    import httpx

    sys.path.insert(0, str(ROOT / "src"))
    from benthic_mcp.bdp import BdpRepository
    from benthic_mcp.catalog import Catalog
    from benthic_mcp.config import Settings

    settings = Settings.from_env()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            return Catalog(await BdpRepository(settings, client).load())
    except Exception:  # noqa: BLE001 - no manifest means nothing can be verified
        return None


def verify_against_manifest(candidates: dict[str, list[dict[str, Any]]], catalog) -> dict[str, Any]:
    """Every identifier a model guessed must exist in the signed manifest to become guidance.

    A guess that turns out to be real is worth writing down; a guess that was wrong is worth
    writing down too, as an anti-pattern. Anything that cannot be resolved either way is recorded
    as needing a human, because guessing at the difference is how bad guidance gets written.
    """
    known_columns: set[str] = set()
    known_relations: set[str] = set()
    if catalog is not None:
        for (dataset, relation), definition in catalog.relations.items():
            known_relations.add(f"{dataset}.{relation}")
            known_relations.add(relation)
            known_columns.update(definition.columns)

    verified: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for name, hits in candidates.items():
        counts: dict[str, int] = defaultdict(int)
        for hit in hits:
            counts[hit["match"]] += 1
        for identifier, times in counts.items():
            if not identifier:
                continue
            entry = {
                "kind": name,
                "identifier": identifier,
                "times_seen": times,
                "examples": [f"{h['probe']}@T{h['turn']}" for h in hits if h["match"] == identifier][:3],
            }
            known = identifier in known_columns or identifier in known_relations
            if known:
                entry["verdict"] = "confirmed"
                verified.append(entry)
            elif catalog is None:
                entry["verdict"] = "unverified-no-manifest"
                unresolved.append(entry)
            else:
                entry["verdict"] = "not-in-manifest"
                rejected.append(entry)

    for bucket in (verified, rejected, unresolved):
        bucket.sort(key=lambda item: item["times_seen"], reverse=True)
    return {"verified": verified, "rejected": rejected, "unresolved": unresolved}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", nargs="*", default=[], help="results.jsonl files from sweep.py")
    parser.add_argument("--out", default=str(ROOT / "eval" / "observer" / "stumbles.json"))
    parser.add_argument("--min-seen", type=int, default=2, help="ignore a one-off")
    args = parser.parse_args()

    paths = [Path(item) for item in args.records] or sorted((ROOT / "eval" / "observer" / "records").glob("*.jsonl"))
    records = load_records(paths)
    if not records:
        print(f"no records found in {[str(p) for p in paths]}", flush=True)
        return 1

    candidates = stumbles(records)
    report = verify_against_manifest(candidates, asyncio.run(signed_catalog()))
    report["records_read"] = len(records)
    report["records_by_probe"] = {
        probe: {
            "answered": sum(1 for r in records if r.get("id") == probe and r.get("answered")),
            "total": sum(1 for r in records if r.get("id") == probe),
            "turn_exhausted": sum(1 for r in records if r.get("id") == probe and r.get("exhausted_turns")),
        }
        for probe in sorted({r.get("id") for r in records if r.get("id")})
    }
    report["candidates"] = [
        item for item in report["verified"] + report["unresolved"] if item["times_seen"] >= args.min_seen
    ]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(
        f"records {len(records)} | confirmed {len(report['verified'])} "
        f"not-in-manifest {len(report['rejected'])} unresolved {len(report['unresolved'])}"
    )
    for item in report["candidates"][:12]:
        print(
            f"  {item['verdict']:22} {item['kind']:22} {item['identifier']!r:26} x{item['times_seen']}  "
            f"{', '.join(item['examples'][:2])}"
        )
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
