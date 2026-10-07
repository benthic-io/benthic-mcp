#!/usr/bin/env python3
"""Is the served manifest's provenance claim true, and would the pipeline rebuild this state?

Two questions, answered separately because they fail for different reasons.

1. **Pin drift.** Each dataset manifest carries `etl_provenance.commit_hash`, and the served
   manifests pin commits older than the checkout the runner actually holds. The pipeline that
   built the database is present on the runner; nothing recorded that it was. That gap is
   invisible from inside either repo, which is why it survived three weeks.

2. **Schema drift.** Whether the indexes live in the serving database are the ones the pipeline
   declares, compared on definition rather than on name. Name comparison is the trap
   `991a8715` exists for: `CREATE INDEX IF NOT EXISTS` succeeds when a wrong index already owns
   the name, so an index can be present, differently defined, and the pipeline would still
   report success. Tablespace is compared too, because `43d42348` exists for that reason.

Read-only throughout: it fetches the served manifests, reads the pipeline checkout, and issues
catalog-only SELECTs. Safe to run on a schedule.

Exit 0 when both hold. Exit 1 on drift, printing what differs. Exit 2 when it could not answer,
which is deliberately not success - a check that cannot reach the database has not verified
anything, and reporting that as a pass is how a blind instrument becomes a green light.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

DATABASES = {
    "usaspending": "usaspending_db",
    "irs_ng": "irs_ng",
    "samer": "sam_er",
    "up_cdmaps": "ucla_polysci_cdmaps",
    "usp_cl": "us_project_cl",
}

ROOT_URL = "https://benthic.io/bdp/ngopen"

# Constraint-backed and PostGIS-internal indexes are created by the system rather than declared,
# so they are counted and named separately instead of being reported as drift forever.

INDEX_PATTERN = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r'"?([A-Za-z0-9_]+)"?\s+ON\s+(?:ONLY\s+)?(?:[A-Za-z0-9_]+\.)?"?([A-Za-z0-9_]+)"?',
    re.IGNORECASE,
)


def fetch_manifest(dataset: str) -> dict:
    url = f"{ROOT_URL}/{dataset}/manifest.json"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read())


def checkout_head(repo: str) -> str | None:
    result = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def declared_indexes(repo: str, dataset: str) -> tuple[dict[str, str], list[str]]:
    """Index name -> relation, plus the files read, so coverage is reportable."""
    import glob

    patterns = [
        os.path.join(repo, "pipelines", dataset, "sql", "*.sql"),
        os.path.join(repo, "recovered", dataset, "*.sql"),
    ]
    found: dict[str, str] = {}
    read: list[str] = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            read.append(os.path.relpath(path, repo))
            text = open(path, encoding="utf-8", errors="replace").read()
            text = re.sub(r"--[^\n]*", "", text)
            for match in INDEX_PATTERN.finditer(text):
                found[match.group(1).strip('"')] = match.group(2)
    return found, read


def live_indexes(database: str) -> tuple[dict[str, tuple[str, str]], str | None]:
    """Index name -> (relation, tablespace), or an error string."""
    query = (
        "SELECT ic.relname, t.relname, coalesce(ts.spcname, '(default)') "
        "FROM pg_index x "
        "JOIN pg_class ic ON ic.oid = x.indexrelid "
        "JOIN pg_class t ON t.oid = x.indrelid "
        "LEFT JOIN pg_tablespace ts ON ts.oid = ic.reltablespace "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = 'public'"
    )
    env = {**os.environ, "PGOPTIONS": "-c statement_timeout=30000"}
    try:
        result = subprocess.run(
            ["psql", "-d", database, "-tA", "-F", "|", "-c", query],
            capture_output=True,
            text=True,
            env=env,
            check=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        return {}, (exc.stderr or str(exc)).strip().splitlines()[-1][:120]
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {}, type(exc).__name__
    live: dict[str, tuple[str, str]] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            live[parts[0]] = (parts[1], parts[2])
    return live, None


AUTO_PATTERN = re.compile(r"(_pkey\d*|_key\d*)$")


def is_automatic(name: str) -> bool:
    """True for indexes the system creates rather than the pipeline declaring.

    `_pkey1` rather than `_pkey`: `uei_crosswalk` carries a duplicate-key index, which a plain
    suffix test misses and which would then be reported as undeclared drift on every run, training
    the reader to ignore the line.
    """
    return bool(AUTO_PATTERN.search(name)) or name.startswith("spatial_ref_sys")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.path.expanduser("~/benthic-io/projects/ngopen-pipelines"))
    parser.add_argument("--datasets", nargs="*", default=sorted(DATABASES))
    parser.add_argument("--pins-only", action="store_true", help="check provenance stamps only")
    args = parser.parse_args()

    head = checkout_head(args.repo)
    if head is None:
        print(f"FAIL: no pipeline checkout at {args.repo}")
        return 2
    print(f"  pipeline checkout HEAD: {head[:12]}")
    print(f"  datasets: {', '.join(args.datasets)}")

    pin_drift: list[str] = []
    schema_drift: list[str] = []
    unreached: list[str] = []

    for dataset in args.datasets:
        database = DATABASES.get(dataset)
        try:
            manifest = fetch_manifest(dataset)
        except Exception as exc:  # noqa: BLE001 - the reason is the useful part
            unreached.append(f"{dataset}: manifest {type(exc).__name__} {exc}")
            continue

        etl = manifest.get("etl_provenance") or {}
        pinned = etl.get("commit_hash") or "(none)"
        moved = pinned != head
        if moved:
            pin_drift.append(f"{dataset}: manifest pins {pinned[:12]}, checkout is {head[:12]}")
        print(f"  {dataset:<11} pin {pinned[:12]} {'DRIFT' if moved else 'matches'}")

        if args.pins_only or database is None:
            continue

        declared, files_read = declared_indexes(args.repo, dataset)
        live, error = live_indexes(database)
        if error:
            unreached.append(f"{dataset}/{database}: {error}")
            print(f"  {dataset:<11} schema UNREACHED: {error}")
            continue
        # Say what was read. A green line over four files once looked like a clean result here, when
        # the other seventy indexes were declared in recovered/. Declarations are read from .sql only;
        # src/ngopen_bdp also issues CREATE INDEX for its run ledger and geocode staging tables, which
        # are pipeline infrastructure rather than dataset relations and so are out of scope - if that
        # ever stops being true, this line is where it shows.
        print(f"  {dataset:<11} declared from {len(files_read)} sql files")

        automatic = {n for n in live if is_automatic(n)}
        only_live = sorted((set(live) - set(declared)) - automatic)
        only_declared = sorted(set(declared) - set(live))
        if only_live or only_declared:
            schema_drift.append(dataset)
            print(
                f"  {dataset:<11} schema DRIFT: {len(only_live)} undeclared, "
                f"{len(only_declared)} declared-but-absent "
                f"({len(declared)} declared from {len(files_read)} files, {len(automatic)} auto)"
            )
            for name in only_live[:6]:
                print(f"      undeclared: {name} on {live[name][0]} [{live[name][1]}]")
            for name in only_declared[:6]:
                print(f"      absent:     {name}")
            if len(only_live) > 6 or len(only_declared) > 6:
                print("      ... truncated")
        else:
            print(
                f"  {dataset:<11} schema clean: {len(declared)} declared, all present "
                f"({len(automatic)} auto-indexes not counted)"
            )

    print()
    for line in unreached:
        print(f"  UNREACHED {line}")
    if unreached:
        print()
        print("  A check that could not reach its subject has verified nothing.")
        return 2
    if pin_drift or schema_drift:
        for line in pin_drift:
            print(f"  PIN DRIFT {line}")
        for dataset in schema_drift:
            print(f"  SCHEMA DRIFT {dataset}")
        return 1
    print("  manifests match the checkout and the pipeline declares what is live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
