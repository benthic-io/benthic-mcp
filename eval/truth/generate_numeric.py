"""Derive numeric expectations from the database and write them to eval/truth/numeric_cases.json.

Direct PostgREST, never `benthic_query`. A number fetched through the server under test inherits every
defect the case exists to catch, which is how a suite becomes a rubber stamp: the expectation would
describe whatever the server happened to say.

Three kinds, chosen because they catch three different failures:

- **filtered_count** - "how many rows have X = v". Catches a filter that silently matches nothing. A
  filter on the wrong column returns 0, and 0 is confident.
- **ordered_max** - "the largest Y in relation Z". Catches the page-local `order=` bug directly: the
  value comes from SQL's ORDER BY, so a server sorting one page in Python cannot reproduce it.
- **grouped_max** - "which group has the largest total". Catches both the sort and an aggregate that
  runs over a partial scan.

Every value records where it came from and when, so a stale expectation is visible rather than silent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from benthic_mcp.catalog import RelationDefinition  # type: ignore[import-not-found]
from benthic_mcp.postgrest import PostgrestTransport  # type: ignore[import-not-found]

# `Range` matters: without it PostgREST ignores the count on a HEAD and reports the full match
# instead, which is the bug count_rows was written to avoid.
_COUNT_HEADERS = {"Prefer": "count=exact", "Range": "0-0"}

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
DEFAULT_OUT = ROOT / "eval" / "truth" / "numeric_cases.json"


def bearer() -> str:
    """Deliberately empty.

    The PostgREST endpoint is anonymous, and sending the MCP server's own bearer token to it makes
    PostgREST try to verify a JWT it holds no secret for: `PGRST300 Server lacks JWT secret`. Presenting
    a credential the endpoint does not want is not a harmless extra header - it is the whole request
    failing, and the failure looks like a missing relation rather than bad auth.
    """
    return ""


async def definition_for(dataset: str, relation: str) -> RelationDefinition | None:
    from benthic_mcp.bdp import BdpRepository  # type: ignore[import-not-found]
    from benthic_mcp.catalog import Catalog  # type: ignore[import-not-found]
    from benthic_mcp.config import Settings  # type: ignore[import-not-found]

    settings = Settings.from_env()
    async with httpx.AsyncClient(timeout=30.0) as client:
        snapshot = await BdpRepository(settings, client).load()
    catalog = Catalog(snapshot)
    return catalog.relations.get((dataset, relation))


def case_id(dataset: str, relation: str, column: str, sample: Any) -> str:
    """A count case's id, carrying the value it counted.

    The first version used `abs(hash(sample)) % 10**6`, and Python randomises string hashing per
    process - so every regeneration minted new ids. A stored run keys its per-case results by id, so
    the ids silently stopped matching and re-grading an old run against a corrected grader stopped
    being possible. Nothing errored; a capability just quietly became unmeasurable.

    The sample itself is the suffix. It is stable, it is readable, and it distinguishes two counts of
    the same column, which the column name alone does not.
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "_", str(sample)).strip("_") or "null"
    return f"count_{dataset}_{relation}_{column}_{safe}"


def relation_url(definition: RelationDefinition) -> str:
    """The relation's URL, built by the same code the server under test builds it with.

    This started as a local `f"{prefix.rstrip('/')}/{relation}"`, which happened to agree with
    `PostgrestTransport._relation_url` and would not have said so. The manifest's `endpoint` is a prefix,
    not a relation URL, and a generator that skips appending the relation name requests the site root
    - which answers 200 with the homepage HTML, so every derived value becomes a JSON decode error
    rather than a wrong number.

    Reusing the server's own helper removes the possibility of the two drifting apart. If they did, the
    generator would be establishing ground truth against a different URL than the server queries, and
    the expectations would be confidently wrong - which is the one failure mode a generator exists to
    rule out.
    """
    return PostgrestTransport._relation_url(definition)


async def count_rows(client: httpx.AsyncClient, endpoint: str, filters: dict[str, str], column: str) -> int | None:
    params: dict[str, str] = {"select": column, "limit": "1"}
    params.update({key: f"eq.{value}" for key, value in filters.items()})
    response = await client.head(endpoint, params=params, headers=_COUNT_HEADERS)
    if response.status_code >= 400:
        return None
    match = re.search(r"/(\d+)$", response.headers.get("content-range", ""))
    return int(match.group(1)) if match else None


async def ordered_max(client: httpx.AsyncClient, endpoint: str, column: str, where: dict[str, str]) -> float | None:
    """The largest value, via SQL's own ORDER BY.

    `.desc.nullslast`, not `.desc`. Postgres sorts NULLS FIRST on a descending sort by default, so
    `order=f990_total_assets_recent.desc limit 1` returns `null` on a column where most rows are null and
    the generator reads "no answer" and emits no case. That column holds a real maximum of
    117,961,275,629, which only appears with the nulls-last ordering.

    The defect never produced a wrong number, which is what made it quiet: it dropped cases, and a
    dropped case leaves nothing behind in a suite that reports only what it generated.
    """
    params: dict[str, str] = {"select": column, "order": f"{column}.desc.nullslast", "limit": "1"}
    params.update({key: f"eq.{value}" for key, value in where.items()})
    response = await client.get(endpoint, params=params)
    if response.status_code >= 400:
        return None
    rows = response.json()
    if not rows:
        return None
    value = rows[0].get(column)
    return float(value) if isinstance(value, (int, float)) else None


async def grouped_max(
    client: httpx.AsyncClient,
    endpoint: str,
    group_column: str,
    value_column: str,
    key: str,
    page: int = 1000,
) -> dict[str, Any] | None:
    """The group with the largest total, and that total, over every row.

    The first version read one ordered page of `limit` rows and summed it, on the reasoning that a
    generator establishing ground truth should use the smallest surface that cannot be wrong in an
    interesting way. That reasoning was wrong about which surface that is. `reporting_agency_overview`
    holds 10,545 rows; reading 200 of them ordered by `toptier_code` and summing produced a total low
    by an order of magnitude that **named the wrong winner**, and the first real run of this capability
    graded a correct model as wrong because of it. The failure looked like a model failure because the
    grader is not supposed to be the thing that is wrong.

    So: read every row, prove it, and sum exactly.

    - The exact row count comes from `Prefer: count=exact`, and without it no case is emitted - an
      aggregate whose coverage cannot be verified is an assertion, not ground truth.
    - Paging stops at that count. A page that comes back short means the two disagree, and the
      aggregate is discarded rather than reported.
    - Money is summed as `Decimal` built from the string PostgREST sent. Summing 10,545 currency rows
      in float64 drifted by cents, and `psql` disagreed by exactly that.
    - Completeness is measured in **distinct row identities**, not rows served. Counting served rows
      is satisfied by repetition: a server that ignores `offset` returns the first page forever, and
      the loop reached 10,545 "rows read" by summing one row ten thousand times.
    """
    total_rows = await count_all(client, endpoint)
    if total_rows is None:
        return None
    totals: dict[Any, Decimal] = {}
    seen: set[Any] = set()
    offset = 0
    while len(seen) < total_rows:
        response = await client.get(
            endpoint,
            params={
                "select": f"{key},{group_column},{value_column}",
                "limit": str(page),
                "offset": str(offset),
                "order": f"{key}.asc",
            },
        )
        if response.status_code >= 400:
            return None
        rows = response.json()
        if not rows:
            return None
        before = len(seen)
        for row in rows:
            identity = row.get(key)
            if identity is None:
                continue
            # Coverage counts every row that exists, not every row that contributed to the sum. 3,353
            # of this relation's 10,545 rows have a null `total_dollars_obligated_gtas`, and counting
            # only the contributing ones capped coverage at 7,192 against a count of 10,545 - so the
            # generator paged to the end and then, correctly, refused to report what it could not
            # prove it had read.
            seen.add(identity)
            value = row.get(value_column)
            group = row.get(group_column)
            if group is None or not isinstance(value, (int, float)):
                continue
            totals[group] = totals.get(group, Decimal(0)) + Decimal(str(value))
        # A page that introduces no new identity means the endpoint is not honouring `offset`, or the
        # count is wrong. Either way there is nothing to gain by asking again, and without this the
        # loop never terminates: `seen` cannot reach `total_rows` and `rows` is never empty.
        if len(seen) == before:
            return None
        offset += len(rows)
    if len(seen) != total_rows:
        return None
    if not totals:
        return None
    winner = max(totals, key=lambda k: totals[k])
    return {
        "group": str(winner),
        "total": totals[winner],
        "rows_read": len(seen),
        "rows_total": total_rows,
    }


async def count_all(client: httpx.AsyncClient, endpoint: str) -> int | None:
    """Every row in the relation, from the endpoint rather than from counting what we read."""
    response = await client.head(endpoint, params={"select": "*", "limit": "1"}, headers=_COUNT_HEADERS)
    if response.status_code >= 400:
        return None
    match = re.search(r"/(\d+)$", response.headers.get("content-range", ""))
    return int(match.group(1)) if match else None


def scan_limit() -> int:
    """The server's complete-scan ceiling, read from its own configuration.

    An aggregate or a join needs every source scanned in full, and `benthic_query` refuses above this
    rather than returning a partial aggregate. A case above it is not a hard question - it is
    unanswerable, and grading it as a failure grades a correct refusal as wrong.
    """
    return int(os.environ.get("BENTHIC_AGGREGATE_SCAN_LIMIT", "10000"))


def unreachable_because(definition: RelationDefinition, rows: int) -> str | None:
    """Why an aggregate over this relation cannot be computed, or None if it can.

    Two independent reasons, and the second does not yield to the first: a relation declaring no
    primary key cannot be paged deterministically, so an aggregate over it is refused once more than
    one page matches, and narrowing below the scan limit does not escape it. Measured on
    `usaspending.reporting_agency_overview`: 10,545 rows is refused by the scan cap, and 1,221 - well
    under it - is still refused for the missing primary key.
    """
    limit = scan_limit()
    reasons = []
    if not definition.primary_key:
        reasons.append(
            f"{definition.dataset}.{definition.name} declares no primary key, so its rows cannot be "
            "paged deterministically and an aggregate over them is refused however far you narrow"
        )
    if rows > limit:
        reasons.append(f"{rows:,} rows exceed the complete-scan limit of {limit:,}")
    return " and ".join(reasons) or None


def money(value: float) -> dict[str, Any]:
    """Tolerances for a currency figure, declared rather than implied.

    Rounding to the cent, and a model writing the figure in billions, are both the same answer. One
    part in a thousand covers both without admitting a different number.
    """
    return {"relative_tolerance": 0.001, "absolute_tolerance": 0.01}


def whole(value: float) -> dict[str, Any]:
    return {"relative_tolerance": 0.0, "absolute_tolerance": 0.0}


async def build(args: argparse.Namespace) -> list[dict[str, Any]]:
    token = bearer()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    derived_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    cases: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=120.0, headers=headers) as client:
        # --- filtered counts. A filter on the wrong column returns 0, and 0 is confident.
        for dataset, relation, column in args.count_targets:
            definition = await definition_for(dataset, relation)
            if definition is None or definition.endpoint is None:
                print(f"  skip {dataset}.{relation}: not in the signed manifest", flush=True)
                continue
            probe = await client.get(
                relation_url(definition),
                params={"select": column, "limit": "1", "order": f"{column}.asc"},
            )
            if probe.status_code >= 400 or not probe.json():
                print(f"  skip {dataset}.{relation}.{column}: not queryable", flush=True)
                continue
            sample = probe.json()[0].get(column)
            if sample is None:
                continue
            total = await count_rows(client, relation_url(definition), {column: str(sample)}, column)
            if total is None:
                continue
            cases.append(
                {
                    "id": case_id(dataset, relation, column, sample),
                    "capability": "numeric_aggregate",
                    "question": (
                        f"How many rows in {dataset}.{relation} have {column} exactly {sample}? Give the number."
                    ),
                    "expected": {
                        "relation": f"{dataset}.{relation}",
                        "kind": "filtered_count",
                        "column": column,
                        "value": total,
                        "sample": str(sample),
                        **whole(total),
                    },
                    "forbidden_claims": [],
                    "required_tools": ["benthic_query"],
                    "derived_from": f"HEAD {relation}?{column}=eq.{sample} with Prefer: count=exact",
                    "derived_at": derived_at,
                }
            )
            print(f"  count {dataset}.{relation}.{column} = {total}", flush=True)

        # --- ordered maxima. The case that catches a page-local ORDER BY.
        for dataset, relation, column, where in args.max_targets:
            definition = await definition_for(dataset, relation)
            if definition is None or definition.endpoint is None:
                print(f"  skip {dataset}.{relation}: not in the signed manifest", flush=True)
                continue
            value = await ordered_max(client, relation_url(definition), column, where)
            if value is None:
                print(f"  skip {dataset}.{relation}.{column}: no answer", flush=True)
                continue
            scope = ", ".join(f"{k}={v}" for k, v in where.items()) or "the whole relation"
            cases.append(
                {
                    # The scope belongs in the id: a filtered and an unfiltered maximum over the same
                    # column produced two cases with the same id, which would silently overwrite one
                    # in any dict keyed by case id.
                    "id": f"max_{dataset}_{relation}_{column}" + (f"_{'_'.join(sorted(where))}" if where else "_all"),
                    "capability": "numeric_aggregate",
                    "question": (
                        f"What is the largest {column} in {dataset}.{relation} over {scope}? Give the figure."
                    ),
                    "expected": {
                        "relation": f"{dataset}.{relation}",
                        "kind": "ordered_max",
                        "column": column,
                        "value": value,
                        "where": where,
                        **money(value),
                    },
                    "forbidden_claims": [],
                    "required_tools": ["benthic_query"],
                    "derived_from": f"GET {relation}?select={column}&order={column}.desc&limit=1",
                    "derived_at": derived_at,
                }
            )
            print(f"  max {dataset}.{relation}.{column} = {value:,.2f}", flush=True)

        # --- grouped totals. Catches an aggregate over a partial scan.
        for dataset, relation, group_column, value_column, declared_key in args.group_targets:
            definition = await definition_for(dataset, relation)
            if definition is None or definition.endpoint is None:
                continue
            # The primary key is what makes the aggregate verifiable: distinct identities are
            # countable, so "did I read the whole relation" has an answer that repetition cannot fake.
            # The manifest's key is authoritative where it exists. Where it is empty - and
            # `reporting_agency_overview` declares none while plainly having
            # `reporting_agency_overview_id` - an operator may name the column, and it is recorded in
            # `derived_from` rather than being applied silently.
            primary = definition.primary_key[0] if definition.primary_key else declared_key
            if primary is None:
                print(
                    f"  skip {dataset}.{relation}: no primary key in the manifest and none declared, "
                    "so coverage of the aggregate could not be verified",
                    flush=True,
                )
                continue
            if not definition.primary_key:
                print(
                    f"  note {dataset}.{relation}: manifest declares no primary key; "
                    f"verifying coverage against declared column {primary}",
                    flush=True,
                )
            outcome = await grouped_max(client, relation_url(definition), group_column, value_column, key=primary)
            if outcome is None:
                continue
            # A question above the ceiling is not hard, it is unanswerable, and the correct answer is
            # the refusal. The derived value stays in the case so the grader can require its absence.
            ceiling = unreachable_because(definition, outcome["rows_total"])
            if ceiling:
                print(f"  unanswerable by design: {ceiling}", flush=True)
            cases.append(
                {
                    "id": f"group_{dataset}_{relation}_{group_column}",
                    "capability": "numeric_aggregate",
                    "question": (
                        f"Across every row in {dataset}.{relation}, group by {group_column} and sum "
                        f"{value_column}. Which {group_column} has the largest total over the whole "
                        "relation, and what is that total? Sum across all periods, not one fiscal "
                        "year or period."
                    ),
                    "expected": {
                        "relation": f"{dataset}.{relation}",
                        "kind": "grouped_max",
                        "column": value_column,
                        "group_column": group_column,
                        "value": float(outcome["total"]),
                        "value_exact": str(outcome["total"]),
                        "group": outcome["group"],
                        "answerable": ceiling is None,
                        **({"ceiling": ceiling} if ceiling else {}),
                        "rows_read": outcome["rows_read"],
                        "rows_total": outcome["rows_total"],
                        **money(outcome["total"]),
                    },
                    "forbidden_claims": [],
                    "required_tools": ["benthic_query"],
                    "derived_from": (
                        f"GET {relation}?select={primary},{group_column},{value_column}, paged by offset "
                        f"until {outcome['rows_total']} distinct {primary} values were read, summed in "
                        "the generator as Decimal"
                    ),
                    "derived_at": derived_at,
                }
            )
            print(f"  group {dataset}.{relation}: winner {outcome['group']} = {outcome['total']:,.2f}", flush=True)

    return cases


def parse_targets(pairs: list[str], columns: int) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for pair in pairs:
        out.append(tuple(pair.split(",")))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--count-target",
        action="append",
        default=[],
        help="dataset,relation,column - repeated",
    )
    parser.add_argument(
        "--max-target",
        action="append",
        default=[],
        help="dataset,relation,column[,col=value,...] - repeated",
    )
    parser.add_argument(
        "--group-target",
        action="append",
        default=[],
        help="dataset,relation,group_column,value_column[,key_column] - repeated",
    )
    args = parser.parse_args()
    args.count_targets = parse_targets(args.count_target, 3)
    args.max_targets = []
    for pair in args.max_target:
        parts = pair.split(",")
        where: dict[str, str] = {}
        for extra in parts[3:]:
            if "=" in extra:
                key, _, value = extra.partition("=")
                where[key] = value
        args.max_targets.append((parts[0], parts[1], parts[2], where))
    args.group_targets = [
        (parts[0], parts[1], parts[2], parts[3], parts[4] if len(parts) > 4 else None)
        for parts in (p.split(",") for p in args.group_target)
    ]

    cases = asyncio.run(build(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {"metadata": {"generated": time.strftime("%Y-%m-%d"), "cases": len(cases)}, "cases": cases}, indent=2
        )
        + "\n"
    )
    print(f"\n  {len(cases)} numeric cases -> {args.output}")


if __name__ == "__main__":
    main()
