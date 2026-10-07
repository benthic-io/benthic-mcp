#!/usr/bin/env python3
"""What the model got wrong, taken from the server's own rejections rather than from its prose.

The first version of this pattern-matched the reasoning for phrases like "let me try" and found 166
of them, which is not a finding - it is filler. The useful signal is already written down by the
server: when a call is refused, the refusal names the identifier that was wrong. That is ground
truth, it is cheap to read, and it cannot be talked into looking like something it is not.

So this walks each transcript in order, pairs every refused call with the error it produced, and
pulls out two things:

  - identifiers the model used that exist nowhere in the signed manifest. These are
    hallucinations, and each is worth an anti-pattern in the guidance.
  - refusals that recur across probes. The model hitting the same wall every session is exactly
    what standing guidance exists to prevent.

Every identifier is checked against the live manifest before it is reported, because the whole
point of this file is that "the model thought so" is not evidence.

Writes eval/observer/findings.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# Refusals whose text names the identifier that caused them.
_IDENTIFIER_ERRORS = [
    (re.compile(r"Unknown columns? for (?P<relation>[\w.]+): (?P<names>[\w, `]+)"), "unknown_column"),
    (re.compile(r"Unknown relation '(?P<name>[\w.]+)'"), "unknown_relation"),
    (re.compile(r"Unknown dataset (?P<name>[\w-]+)"), "unknown_dataset"),
    (re.compile(r"Unknown aggregate function '(?P<name>[\w-]+)'"), "unknown_function"),
    (re.compile(r"Unknown filter operator '(?P<name>[\w-]+)'"), "unknown_operator"),
]
# Refusals that recur are a different finding: not a hallucination, but a wall the model keeps
# hitting, which is what guidance is for.
_WALL = re.compile(
    r"(?P<what>complete-scan limit|rows match the filters|no primary key|"
    r"exceeds \d+ bytes|cannot produce reliable)"
)


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


_SERVER_ERROR = re.compile(r"returned (5\d\d) for ")


def only_server_errors(record: dict[str, Any]) -> bool:
    """True when every failed call in this case came back as a 5xx.

    A case the server answered only with 503s is an outage whatever the file says about turns, and
    is_invalid() has to catch it: left valid, its refusals are counted as evidence about the model.
    Forty-five such cases from 2026-10-01 put 429 outage errors into refusal_kinds, which read as a
    live 503 defect that does not exist.

    Requires at least one failed call, so a case that simply never called anything is not here; that
    is is_invalid()'s existing zero-turn rule. One genuine refusal among the 5xx calls means the
    server did answer something, and the case is about the model again.
    """
    results = [result for turn in record.get("turns") or [] for result in turn.get("tool_results") or []]
    failed = [result for result in results if not result.get("ok", True)]
    return bool(failed) and all(_SERVER_ERROR.search(result.get("text") or "") for result in failed)


def is_invalid(record: dict[str, Any]) -> bool:
    """True when the server never answered this case.

    Derived rather than trusted, because most records predate the field: a case with no turns that ran
    to the tool timeout is an outage whatever the file says. The explicit flag wins when present so a
    future change to the rule is not silently overridden by this fallback.
    """
    if "invalid" in record:
        return bool(record["invalid"])
    if only_server_errors(record):
        return True
    elapsed = record.get("elapsed_s")
    try:
        elapsed = float(elapsed) if elapsed is not None else 0.0
    except (TypeError, ValueError):
        elapsed = 0.0
    return not record.get("turns") and elapsed >= 285.0


def identifiers_in(error_text: str) -> list[tuple[str, str]]:
    """(kind, identifier) pairs a refusal is complaining about."""
    found: list[tuple[str, str]] = []
    for pattern, kind in _IDENTIFIER_ERRORS:
        match = pattern.search(error_text)
        if not match:
            continue
        groups = match.groupdict()
        if kind == "unknown_column":
            for name in re.split(r"[,\s]+", groups.get("names", "")):
                cleaned = name.strip().strip("`")
                if cleaned and cleaned not in {"and", "or"}:
                    found.append((kind, cleaned))
        else:
            name = groups.get("name", "").strip()
            if name:
                found.append((kind, name))
    return found


def survey(records: list[dict[str, Any]]) -> dict[str, Any]:
    hallucinated: dict[tuple[str, str], list[str]] = defaultdict(list)
    walls: Counter = Counter()
    wall_examples: dict[str, list[str]] = defaultdict(list)
    refusals: Counter = Counter()

    for record in records:
        # An outage is not evidence about the model, so its errors never reach the counters. Kept in
        # the record set and counted separately as invalid_total, which is what makes a refusal a
        # measurement rather than a tally of outages.
        if is_invalid(record):
            continue
        probe = record.get("id", "?")
        for turn in record.get("turns", []):
            for result in turn.get("tool_results", []):
                text = result.get("text", "")
                error = re.search(r'"error":\s*"(?P<message>.*?)"\s*\}?$', text.strip(), re.S)
                message = error.group("message") if error else ""
                if not message and '"error"' in text[:200]:
                    message = text[:200]
                if not message:
                    continue
                refusals[message.split(".")[0][:90]] += 1
                for kind, name in identifiers_in(message):
                    hallucinated[(kind, name)].append(f"{probe}@T{turn.get('turn')}")
                wall = _WALL.search(message)
                if wall:
                    key = wall.group("what")
                    walls[key] += 1
                    if len(wall_examples[key]) < 3:
                        wall_examples[key].append(f"{probe}@T{turn.get('turn')}: {message[:110]}")

    return {"hallucinated": hallucinated, "walls": walls, "wall_examples": wall_examples, "refusals": refusals}


async def signed_catalog():
    import sys

    import httpx

    sys.path.insert(0, str(ROOT / "src"))
    from benthic_mcp.bdp import BdpRepository
    from benthic_mcp.catalog import Catalog
    from benthic_mcp.config import Settings

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            return Catalog(await BdpRepository(Settings.from_env(), client).load())
    except Exception:  # noqa: BLE001
        return None


def check(catalog, hallucinated: dict[tuple[str, str], list[str]]) -> dict[str, list[dict[str, Any]]]:
    known_columns: set[str] = set()
    known_relations: set[str] = set()
    if catalog is not None:
        for (dataset, relation), definition in catalog.relations.items():
            known_relations.update({f"{dataset}.{relation}", relation})
            known_columns.update(definition.columns)

    absent: list[dict[str, Any]] = []
    present_elsewhere: list[dict[str, Any]] = []
    for (kind, name), where in sorted(hallucinated.items(), key=lambda item: -len(item[1])):
        entry = {"kind": kind, "identifier": name, "times": len(where), "where": where[:3]}
        if catalog is None:
            absent.append({**entry, "verdict": "unverified-no-manifest"})
        elif name in known_columns or name in known_relations:
            # Real, but not here. Usually the model assumed a column another relation has.
            present_elsewhere.append({**entry, "verdict": "exists-on-another-relation"})
        else:
            absent.append({**entry, "verdict": "not-in-manifest"})
    return {"absent": absent, "present_elsewhere": present_elsewhere}


def missing_probes(newest_records: list[dict[str, Any]], expected: set[str]) -> list[str]:
    """Probes the newest cycle never ran, sorted.

    Empty means the cycle covered the probe set. Anything else means it was cut short, and the rates
    downstream would be computed over whichever probes happened to finish.
    """
    ran = {str(record.get("id")) for record in newest_records}
    return sorted(expected - ran)


def cycle_is_complete(newest_records: list[dict[str, Any]], expected: set[str]) -> bool:
    return not missing_probes(newest_records, expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", nargs="*", default=[])
    parser.add_argument("--out", default=str(ROOT / "eval" / "observer" / "findings.json"))
    parser.add_argument("--min-seen", type=int, default=1)
    parser.add_argument(
        "--probes",
        type=Path,
        default=ROOT / "eval" / "observer" / "probes" / "core.json",
        help="the probe set the newest cycle is expected to have covered in full",
    )
    args = parser.parse_args()

    paths = [Path(item) for item in args.records]
    if not paths:
        paths = sorted((ROOT / "eval" / "observer" / "records").glob("*.jsonl"))
    records = load_records(paths)
    if not records:
        print(f"no records in {[str(p) for p in paths]}")
        return 1

    # The newest cycle is the one an agent will act on. If the server did not answer during it, its
    # numbers are not a measurement of the model, so refuse to write findings at all. A cycle that
    # never ran and a cycle that ran badly look identical downstream otherwise, and that ambiguity is
    # how a wedged server was reported as a 39% pass rate.
    newest = max((p for p in paths if p.is_file()), key=lambda p: p.stat().st_mtime, default=None)
    newest_records = load_records([newest]) if newest else []
    stale = [r for r in newest_records if is_invalid(r)]
    if stale:
        print(
            f"REFUSING: {len(stale)}/{len(newest_records)} cases in the newest cycle "
            f"({newest.name if newest else '?'}) got no answer from the server: "
            f"{', '.join(sorted({str(r.get('id')) for r in stale}))}"
        )
        print("That is an outage, not a model result. Not writing findings.")
        return 2

    # A cycle that was killed partway leaves a partial record, and a partial record is
    # indistinguishable from a complete one downstream: tick.sh only asks that the file is non-empty,
    # so eleven of seventeen probes produce findings and nothing says the other six never ran.
    # 86 of 102 recorded cycles are short of the full probe set, and the most recent four truncate
    # progressively - 16, 13, 12, 11 cases - because the sweep outgrew TimeoutStartSec=3600 while
    # complete cycles rose from 2385s to 3393s. Reporting rates from those is reporting a partial
    # measurement as a whole one, which is the failure this file exists to prevent.
    if not args.probes.exists():
        print(f"probe set {args.probes} is missing, so the newest cycle cannot be checked")
        return 1
    expected = {str(item.get("id")) for item in json.loads(args.probes.read_text(encoding="utf-8"))["probes"]}
    missing = missing_probes(newest_records, expected)
    if missing:
        ran = {str(record.get("id")) for record in newest_records}
        print(
            f"REFUSING: the newest cycle ({newest.name if newest else '?'}) covered "
            f"{len(ran & expected)}/{len(expected)} probes and never ran {', '.join(missing)}"
        )
        print("A cycle cut short is not a measurement. Not writing findings.")
        return 2

    found = survey(records)
    verdicts = check(asyncio.run(signed_catalog()), found["hallucinated"])
    report = {
        "records_read": len(records),
        # Kept separate from answered rather than folded into it: a case the server never answered is
        # not a failure, and any rate computed over these counts has to exclude them.
        "invalid_total": sum(1 for r in records if is_invalid(r)),
        "per_probe": {
            probe: {
                "answered": sum(1 for r in records if r.get("id") == probe and r.get("answered")),
                "run": sum(1 for r in records if r.get("id") == probe),
                "turn_exhausted": sum(1 for r in records if r.get("id") == probe and r.get("exhausted_turns")),
                "invalid": sum(1 for r in records if r.get("id") == probe and is_invalid(r)),
            }
            for probe in sorted({str(r["id"]) for r in records if r.get("id")})
        },
        "hallucinated_identifiers": verdicts["absent"],
        "identifiers_that_exist_elsewhere": verdicts["present_elsewhere"],
        "recurring_walls": [
            {"what": what, "times": times, "examples": found["wall_examples"][what]}
            for what, times in found["walls"].most_common()
        ],
        "refusal_kinds": found["refusals"].most_common(12),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"records {len(records)}")
    if report["invalid_total"]:
        print(f"  INVALID (server never answered, excluded from every rate): {report['invalid_total']}")
    print(f"\nHALLUCINATED IDENTIFIERS ({len(report['hallucinated_identifiers'])}):")
    for item in report["hallucinated_identifiers"][:14]:
        if item["times"] >= args.min_seen:
            print(f"  {item['kind']:18} {item['identifier']!r:26} x{item['times']:<3} {', '.join(item['where'][:2])}")
    print(f"\nEXISTS ON ANOTHER RELATION ({len(report['identifiers_that_exist_elsewhere'])}):")
    for item in report["identifiers_that_exist_elsewhere"][:10]:
        print(f"  {item['kind']:18} {item['identifier']!r:26} x{item['times']:<3} {', '.join(item['where'][:2])}")
    print("\nRECURRING WALLS:")
    for item in report["recurring_walls"][:8]:
        print(f"  {item['what']:28} x{item['times']}")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
