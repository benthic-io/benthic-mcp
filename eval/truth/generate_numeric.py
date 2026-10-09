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
import sys
import time
from pathlib import Path
from typing import Any

import httpx

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


async def endpoint_for(dataset: str, relation: str) -> str | None:
    from benthic_mcp.bdp import BdpRepository  # type: ignore[import-not-found]
    from benthic_mcp.catalog import Catalog  # type: ignore[import-not-found]
    from benthic_mcp.config import Settings  # type: ignore[import-not-found]

    settings = Settings.from_env()
    async with httpx.AsyncClient(timeout=30.0) as client:
        snapshot = await BdpRepository(settings, client).load()
    catalog = Catalog(snapshot)
    definition = catalog.relations.get((dataset, relation))
    return definition.endpoint if definition else None


def relation_url(prefix: str, relation: str) -> str:
    """The manifest's `endpoint` is a prefix, not a relation URL.

    `postgrest.py:_relation_url` appends the relation name to it, and a generator that skips that step
    requests the site root instead - which answers 200 with the homepage HTML, and every derived value
    becomes a JSON decode error rather than a wrong number.
    """
    return f"{prefix.rstrip('/')}/{relation}"


async def count_rows(client: httpx.AsyncClient, endpoint: str, filters: dict[str, str], column: str) -> int | None:
    params: dict[str, str] = {"select": column, "limit": "1"}
    params.update({key: f"eq.{value}" for key, value in filters.items()})
    response = await client.head(endpoint, params=params, headers={"Prefer": "count=exact", "Range": "0-0"})
    if response.status_code >= 400:
        return None
    import re

    match = re.search(r"/(\d+)$", response.headers.get("content-range", ""))
    return int(match.group(1)) if match else None


async def ordered_max(client: httpx.AsyncClient, endpoint: str, column: str, where: dict[str, str]) -> float | None:
    """The largest value, via SQL's own ORDER BY.

    Deliberately not `limit=1` on a guess: PostgREST applies `order` before `limit`, and the whole
    point is that the ordering happens where every row is visible.
    """
    params: dict[str, str] = {"select": column, "order": f"{column}.desc", "limit": "1"}
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
    client: httpx.AsyncClient, endpoint: str, group_column: str, value_column: str, limit: int
) -> dict[str, Any] | None:
    """The group with the largest total, and that total.

    Fetched in one ordered page and summed in Python rather than asking PostgREST to aggregate: this is
    the generator establishing ground truth, so it uses the smallest surface that cannot be wrong in an
    interesting way, and it is bounded by `limit` on purpose. Where the true answer needs more rows
    than that, no case is emitted rather than a wrong one.
    """
    response = await client.get(
        endpoint,
        params={"select": f"{group_column},{value_column}", "limit": str(limit), "order": f"{group_column}.asc"},
    )
    if response.status_code >= 400:
        return None
    rows = response.json()
    if not rows:
        return None
    totals: dict[Any, float] = {}
    for row in rows:
        key, value = row.get(group_column), row.get(value_column)
        if key is None or not isinstance(value, (int, float)):
            continue
        totals[key] = totals.get(key, 0.0) + float(value)
    if not totals:
        return None
    winner = max(totals, key=lambda k: totals[k])
    return {"group": str(winner), "total": totals[winner], "rows_read": len(rows)}


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
            endpoint = await endpoint_for(dataset, relation)
            if endpoint is None:
                print(f"  skip {dataset}.{relation}: not in the signed manifest", flush=True)
                continue
            probe = await client.get(
                relation_url(endpoint, relation),
                params={"select": column, "limit": "1", "order": f"{column}.asc"},
            )
            if probe.status_code >= 400 or not probe.json():
                print(f"  skip {dataset}.{relation}.{column}: not queryable", flush=True)
                continue
            sample = probe.json()[0].get(column)
            if sample is None:
                continue
            total = await count_rows(client, relation_url(endpoint, relation), {column: str(sample)}, column)
            if total is None:
                continue
            cases.append(
                {
                    "id": f"count_{dataset}_{relation}_{column}_{abs(hash(str(sample))) % 10**6}",
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
            endpoint = await endpoint_for(dataset, relation)
            if endpoint is None:
                print(f"  skip {dataset}.{relation}: not in the signed manifest", flush=True)
                continue
            value = await ordered_max(client, relation_url(endpoint, relation), column, where)
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
        for dataset, relation, group_column, value_column, limit in args.group_targets:
            endpoint = await endpoint_for(dataset, relation)
            if endpoint is None:
                continue
            outcome = await grouped_max(client, relation_url(endpoint, relation), group_column, value_column, limit)
            if outcome is None:
                continue
            cases.append(
                {
                    "id": f"group_{dataset}_{relation}_{group_column}",
                    "capability": "numeric_aggregate",
                    "question": (
                        f"Group {dataset}.{relation} by {group_column} and sum {value_column}. "
                        f"Which {group_column} has the largest total, and what is that total?"
                    ),
                    "expected": {
                        "relation": f"{dataset}.{relation}",
                        "kind": "grouped_max",
                        "column": value_column,
                        "group_column": group_column,
                        "value": outcome["total"],
                        "group": outcome["group"],
                        "rows_read": outcome["rows_read"],
                        **money(outcome["total"]),
                    },
                    "forbidden_claims": [],
                    "required_tools": ["benthic_query"],
                    "derived_from": (
                        f"GET {relation}?select={group_column},{value_column}&order={group_column}.asc"
                        f"&limit={limit}, summed in the generator"
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
        help="dataset,relation,group_column,value_column,limit - repeated",
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
        (parts[0], parts[1], parts[2], parts[3], int(parts[4]) if len(parts) > 4 else 5000)
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
