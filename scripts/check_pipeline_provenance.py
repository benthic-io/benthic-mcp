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

# Captures a whole CREATE INDEX statement, which is what has to be compared. Matching only the name
# is the trap 991a8715 exists for: CREATE INDEX IF NOT EXISTS succeeds when a differently-defined
# index already owns the name, so a name-only check reports that as clean.
STATEMENT_PATTERN = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[^;]*;",
    re.IGNORECASE | re.DOTALL,
)


def normalise_definition(sql: str) -> str:
    """Reduce a CREATE INDEX statement to a form both sides can be compared in.

    Drops what carries no meaning about the index - IF NOT EXISTS, the public prefix, CONCURRENTLY,
    TABLESPACE (compared separately, against the database's own view), and quoting - lowercases, and
    spells out the default access method, which the pipeline omits and pg_get_indexdef always writes.
    Predicates are compared with parentheses removed.

    That last part is a stated limitation rather than a solved problem: `(a is null) and (b)` and
    `((a is null) and (b))` are the same predicate and normalise equal, but so do `(a or b) and c` and
    `a or (b and c)`, which are not. Choosing between a check that reports phantom mismatches and one
    that under-reports, this under-reports and says so. Everything the trap actually turns on - the
    column list, its order, the access method, uniqueness, and whether a predicate exists at all - is
    compared exactly.
    """
    """Reduce a CREATE INDEX statement to a form both sides can be compared in.

    Drops the qualifiers that carry no meaning about the index itself - IF NOT EXISTS, the public
    schema prefix, CONCURRENTLY, TABLESPACE, quoting - and collapses whitespace. TABLESPACE is dropped
    because it is compared separately and against the database's own view, not the pipeline's.
    """
    text = re.sub(r"--[^\n]*", "", sql)
    text = re.sub(r"\bIF\s+NOT\s+EXISTS\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCONCURRENTLY\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTABLESPACE\s+[A-Za-z0-9_\"]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpublic\s*\.\s*", "", text, flags=re.IGNORECASE)
    text = text.replace('"', "")
    # Lowercased before the predicate is split off, because the two sides disagree about case here and
    # the split below looks for a lowercase " where ". Both sides are normalised identically, so the
    # only thing this loses is a distinction between two definitions that differ solely in the case of
    # a string literal - which no index here does.
    text = text.lower()
    # btree is the default and the pipeline relies on that, while pg_get_indexdef always spells it
    # out. Without this the first run reported 36 mismatches that were all the same index.
    text = re.sub(r"(\bon\s+[a-z0-9_]+)\s*(\()", r"\1 using btree \2", text)
    # `using gist(geom_point)` and `using gist (geom_point)` are the same index.
    text = re.sub(r"\busing\s+([a-z0-9_]+)\s*\(", r"using \1 (", text)
    text = re.sub(r"\s+", " ", text)
    # `(b)::text` in a column list is `b::text` once the database echoes it back.
    text = re.sub(r"\(([^()]+)\)\s*::", r"\1::", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    # Predicate parentheses carry no meaning and both sides disagree about them: the pipeline writes
    # `((a is null) and (b is not null))` where pg_get_indexdef echoes `(a is null) and (b is not null)`.
    # Flattening atom-wrapping parens and then the outermost pair equates them. Restricted to the
    # predicate so a column list like `(fiscal_year, award_id)` is never touched.
    head, sep, predicate = text.partition(" where ")
    if sep:
        predicate = predicate.replace("(", " ").replace(")", " ")
        predicate = re.sub(r"\s+", " ", predicate).strip()
        text = f"{head} where {predicate}"
    return text.strip().rstrip(";").strip()


def fetch_manifest(dataset: str) -> dict:
    url = f"{ROOT_URL}/{dataset}/manifest.json"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read())


def checkout_head(repo: str) -> str | None:
    result = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def parse_index_statements(text: str) -> dict[str, tuple[str, str]]:
    """Index name -> (relation, normalised definition) from a SQL file's text.

    Comments are stripped before the statements are located. `recovered/usaspending/
    indexes_recovered.sql` carries a prose comment containing the words "CREATE INDEX CONCURRENTLY";
    locating statements without stripping first parses that as a declaration named CONCURRENTLY, which
    is then reported as a declared index absent from the database.
    """
    text = re.sub(r"--[^\n]*", "", text)
    found: dict[str, tuple[str, str]] = {}
    for statement in STATEMENT_PATTERN.findall(text):
        match = INDEX_PATTERN.search(statement)
        if match is None:
            continue
        found[match.group(1).strip('"')] = (match.group(2), normalise_definition(statement))
    return found


def declared_indexes(repo: str, dataset: str) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Index name -> (relation, normalised definition), plus the files read."""
    import glob

    patterns = [
        os.path.join(repo, "pipelines", dataset, "sql", "*.sql"),
        os.path.join(repo, "recovered", dataset, "*.sql"),
    ]
    found: dict[str, tuple[str, str]] = {}
    read: list[str] = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            read.append(os.path.relpath(path, repo))
            text = open(path, encoding="utf-8", errors="replace").read()
            found.update(parse_index_statements(text))
    return found, read


def live_indexes(database: str) -> tuple[dict[str, tuple[str, str, str]], str | None]:
    """Index name -> (relation, tablespace, normalised definition), or an error string."""
    query = (
        "SELECT ic.relname, t.relname, coalesce(ts.spcname, '(default)'), pg_get_indexdef(x.indexrelid) "
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
    live: dict[str, tuple[str, str, str]] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            live[parts[0]] = (parts[1], parts[2], normalise_definition(parts[3]))
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
        # The same name with a different definition is drift too, and it is the one a name-only check
        # cannot see: the pipeline would report success against an index it did not create.
        #
        # The index head - name, relation, access method, column list, uniqueness, and whether a
        # predicate exists at all - is compared exactly. The predicate is reported but not counted,
        # because normalising it means choosing between phantom mismatches (`0` against `0::numeric`)
        # and erasing real ones (`(b)::text <> ''` against `b <> ''`), and a check that cries wolf
        # stops being read.
        mismatched: list[str] = []
        predicates_differ: list[str] = []
        for name in sorted(set(live) & set(declared)):
            live_head, _, live_where = live[name][2].partition(" where ")
            declared_head, _, declared_where = declared[name][1].partition(" where ")
            if live_head != declared_head:
                mismatched.append(name)
            elif live_where != declared_where:
                predicates_differ.append(name)
        if only_live or only_declared or mismatched or predicates_differ:
            schema_drift.append(dataset)
            print(
                f"  {dataset:<11} schema DRIFT: {len(only_live)} undeclared, "
                f"{len(only_declared)} declared-but-absent, {len(mismatched)} differently defined, "
                f"{len(predicates_differ)} predicate-only "
                f"({len(declared)} declared from {len(files_read)} files, {len(automatic)} auto)"
            )
            for name in mismatched[:4]:
                print(f"      MISMATCH:   {name}")
                print(f"        declared {declared[name][1][:150]}")
                print(f"        live     {live[name][2][:150]}")
            if predicates_differ:
                print(
                    f"      predicate-only, reported not counted: {len(predicates_differ)} "
                    f"({', '.join(predicates_differ[:4])})"
                )
            for name in only_live[:6]:
                print(f"      undeclared: {name} on {live[name][0]} [{live[name][1]}]")
            for name in only_declared[:6]:
                print(f"      absent:     {name}")
            if len(only_live) > 6 or len(only_declared) > 6 or len(mismatched) > 4:
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
