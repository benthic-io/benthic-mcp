import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx

from benthic_mcp.bdp import BdpRepository
from benthic_mcp.catalog import Catalog, RelationDefinition
from benthic_mcp.config import Settings
from benthic_mcp.rpc import RPC_DEFINITIONS

ROOT = Path(__file__).parents[1]


def safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def relation_name(definition: RelationDefinition) -> str:
    return f"{definition.dataset}.{definition.name}"


def scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_.:%/-]+", text):
        return text
    return json.dumps(text, ensure_ascii=True)


def filter_params(column: str, value: Any, column_type: str | None = None) -> list[tuple[str, str]]:
    if column_type == "integer" and isinstance(value, str) and value.isdigit():
        value = int(value)
    return [(column, f"eq.{scalar(value)}")]


def choose_columns(definition: RelationDefinition, preferred: list[str], limit: int = 5) -> list[str]:
    names = [name for name in preferred if name in definition.columns]
    names.extend(name for name in definition.columns if name not in names)
    return names[:limit]


async def fetch_rows(
    client: httpx.AsyncClient,
    definition: RelationDefinition,
    columns: list[str],
    limit: int,
    filters: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if definition.endpoint is None:
        return []
    params: list[tuple[str, str | int]] = [("select", ",".join(columns)), ("limit", limit)]
    if filters:
        grouped: dict[str, list[tuple[str, str]]] = {}
        for column, expression in filters:
            grouped.setdefault(column, []).append((column, expression))
        for column, expressions in grouped.items():
            if len(expressions) == 1:
                params.append((column, expressions[0][1]))
            else:
                params.append(("and", "(" + ",".join(expression for _, expression in expressions) + ")"))
    response = await client.get(
        f"{definition.endpoint}{definition.name}",
        params=httpx.QueryParams([(key, str(value)) for key, value in params]),
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


async def sample_key(
    client: httpx.AsyncClient,
    definition: RelationDefinition,
    column: str,
) -> Any:
    rows = await fetch_rows(client, definition, [column], 5)
    for row in rows:
        value = row.get(column)
        if value is not None and not isinstance(value, (dict, list)):
            return value
    return None


async def direct_rpc(
    client: httpx.AsyncClient,
    catalog: Catalog,
    operation: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    definition = RPC_DEFINITIONS[operation]
    endpoint = catalog.endpoint_for(definition.dataset)
    response = await client.get(f"{endpoint}rpc/{definition.function_name}", params=arguments)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        rows = []
    return {
        "operation": operation,
        "row_count": len(rows),
        "rows": rows[:5],
        "arguments": arguments,
    }


async def coordinate_sample(client: httpx.AsyncClient, catalog: Catalog) -> tuple[float, float] | None:
    for definition in catalog.relations.values():
        if "latitude" not in definition.columns or "longitude" not in definition.columns:
            continue
        try:
            rows = await fetch_rows(client, definition, ["latitude", "longitude"], 1)
        except httpx.HTTPError:
            continue
        if (
            rows
            and isinstance(rows[0].get("latitude"), (int, float))
            and isinstance(rows[0].get("longitude"), (int, float))
        ):
            return float(rows[0]["latitude"]), float(rows[0]["longitude"])
    return None


def make_case(
    case_id: str,
    question: str,
    capability: str,
    expected: dict[str, Any],
    *,
    required_tools: list[str],
    forbidden_claims: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": case_id,
        "question": question,
        "capability": capability,
        "required_tools": required_tools,
        "expected": expected,
        "forbidden_claims": forbidden_claims or [],
    }


_TRAP_NAME_TOKENS = ("current", "latest", "active", "now")
_TEMPORAL_COLUMNS = (("term_start", "term_end"), ("start_date", "end_date"), ("begin_date", "end_date"))


def spread_across_datasets(definitions: list[RelationDefinition], count: int) -> list[RelationDefinition]:
    """Pick relations round-robin across datasets so broader coverage stays balanced.

    Taking the first N in catalog order favours whichever dataset happens to be listed first, which
    is how coverage used to be stuck at one relation per dataset.
    """
    by_dataset: dict[str, list[RelationDefinition]] = {}
    for definition in definitions:
        by_dataset.setdefault(definition.dataset, []).append(definition)
    chosen: list[RelationDefinition] = []
    index = 0
    while len(chosen) < count:
        added = False
        for dataset in sorted(by_dataset):
            bucket = by_dataset[dataset]
            if index < len(bucket) and len(chosen) < count:
                chosen.append(bucket[index])
                added = True
        if not added:
            break
        index += 1
    return chosen


def spread_pairs(pairs: list, count: int) -> list:
    """Same idea for sequential pairs: prefer pairs that cross datasets, which are the harder ones."""
    crossing = [pair for pair in pairs if pair[0].dataset != pair[1].dataset]
    same = [pair for pair in pairs if pair[0].dataset == pair[1].dataset]
    picked: list = []
    for source in (crossing, same):
        for pair in source:
            if len(picked) >= count:
                return picked
            if pair not in picked:
                picked.append(pair)
    return picked


def two_hop_chains(catalog: Catalog) -> list[list]:
    """Chains of two signed join paths that share a hub relation.

    These are the questions that actually need the playbook: the model has to find both paths
    instead of guessing one, and it has to carry an identifier across the hop. A single relation
    participating as the target of one path and the source of another is the only shape that works.
    """
    forward = {
        (join.to_dataset, join.to_relation, join.to_column): join
        for join in catalog.joins
        if join.join_type != "spatial"
    }
    chains: list[list] = []
    for second in catalog.joins:
        if second.join_type == "spatial":
            continue
        hub = (second.from_dataset, second.from_relation)
        for (to_dataset, to_relation, to_column), first in forward.items():
            if (to_dataset, to_relation) != hub:
                continue
            if first.join_type == "spatial":
                continue
            try:
                left = catalog.resolve_relation(first.from_dataset, first.from_relation)
                middle = catalog.resolve_relation(*hub)
                right = catalog.resolve_relation(second.to_dataset, second.to_relation)
            except Exception:
                continue
            if len({first.from_relation, second.to_relation, middle.name}) < 2:
                continue
            chains.append([left, middle, right, first, second])
    return chains


def relation_traps(catalog: Catalog) -> list[tuple[RelationDefinition, RelationDefinition]]:
    """A relation that looks like the obvious answer to a question it cannot actually answer.

    Built generically: a temporal table or view paired with a sibling whose name advertises that it
    only holds the present. Asking about the past and using the current-only view is the mistake
    most likely to produce a confidently wrong answer, which is exactly what the harness cannot
    otherwise detect.
    """
    traps: list[tuple[RelationDefinition, RelationDefinition]] = []
    for dataset in sorted(catalog.datasets):
        relations = [definition for definition in catalog.relations.values() if definition.dataset == dataset]
        current_only = [
            definition
            for definition in relations
            if any(token in definition.name.lower() for token in _TRAP_NAME_TOKENS)
            and definition.queryable
            and definition.endpoint
        ]
        for candidate in current_only:
            temporal = [
                definition
                for definition in relations
                if definition is not candidate
                and definition.queryable
                and definition.endpoint is not None
                and any(left in definition.columns and right in definition.columns for left, right in _TEMPORAL_COLUMNS)
            ]
            if not temporal:
                continue
            # Prefer a temporal relation the signed catalog actually joins on, since those are the
            # ones questions are likely to be asked about.
            joined = {(join.from_dataset, join.from_relation) for join in catalog.joins} | {
                (join.to_dataset, join.to_relation) for join in catalog.joins
            }
            temporal.sort(key=lambda item: ((item.dataset, item.name) not in joined, item.name))
            traps.append((temporal[0], candidate))
    return traps


async def generate(settings: Settings, args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        repository = BdpRepository(settings, client)
        snapshot = await repository.load()
        catalog = Catalog(snapshot)
        cases: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        join_count = 0

        identifier_paths = [join for join in catalog.joins if join.join_type in {"identifier", "heuristic"}]
        for join in identifier_paths:
            left = catalog.resolve_relation(join.from_dataset, join.from_relation)
            right = catalog.resolve_relation(join.to_dataset, join.to_relation)
            try:
                key = await sample_key(client, left, join.from_column)
                if key is None:
                    skipped.append(
                        {"reason": "no sample key", "join": f"{relation_name(left)} -> {relation_name(right)}"}
                    )
                    continue
                matches = await fetch_rows(
                    client,
                    right,
                    [join.to_column],
                    5,
                    filter_params(join.to_column, key, right.columns[join.to_column].type),
                )
            except httpx.HTTPError as exc:
                skipped.append(
                    {"reason": f"probe failed: {exc}", "join": f"{relation_name(left)} -> {relation_name(right)}"}
                )
                continue
            path_id = f"{safe_name(join.from_dataset)}_{safe_name(join.from_relation)}_{safe_name(join.to_dataset)}_{safe_name(join.to_relation)}"
            reliability_text = (
                "Treat these as heuristic matches and clearly label them as non-exact."
                if join.reliability.value == "heuristic"
                else "Include the signed join evidence and state its reliability."
            )
            context_column = None
            context_value = None
            bound_column = None
            bound_value = None
            if join.reliability.value == "partial":
                shared_context = sorted(set(left.columns) & set(right.columns))
                shared_context = [
                    column
                    for column in shared_context
                    if column != join.from_column and left.columns[column].type in {"string", "integer", "date"}
                ]
                for candidate in shared_context:
                    rows = await fetch_rows(
                        client,
                        left,
                        [candidate],
                        1,
                        filter_params(join.from_column, key, left.columns[join.from_column].type),
                    )
                    if rows and rows[0].get(candidate) is not None:
                        context_column = candidate
                        context_value = rows[0][candidate]
                        break
                bound_candidates = [
                    column
                    for column in ["uei", *left.primary_key]
                    if column in left.columns and column != join.from_column
                ]
                for candidate in bound_candidates:
                    rows = await fetch_rows(
                        client,
                        left,
                        [candidate],
                        1,
                        filter_params(join.from_column, key, left.columns[join.from_column].type),
                    )
                    if rows and rows[0].get(candidate) is not None:
                        bound_column = candidate
                        bound_value = rows[0][candidate]
                        break
            context_text = (
                f" Use the sampled {context_column}={context_value!r} context, then call benthic_join."
                if context_column is not None
                else " Retrieve a bounded context row first, then call benthic_join."
            )
            if join.reliability.value == "partial":
                context_text += " Include the context fields and distinguish partial evidence."
            if bound_column is not None:
                context_text += f" Bound the left side with {bound_column}={bound_value!r}."
            left_filters = [f"{join.from_column}=eq.{key}"]
            if bound_column is not None:
                left_filters.append(f"{bound_column}=eq.{bound_value}")
            tool_hint = (
                f" Call benthic_join with left_source={relation_name(left)!r}, right_source={relation_name(right)!r}, "
                f"left_column={join.from_column!r}, right_column={join.to_column!r}, "
                f"left_where={json.dumps(left_filters)}."
            )
            if join.reliability.value == "partial" and context_column is not None:
                tool_hint += f" context_conditions=[{json.dumps(f'{context_column}={context_column}')}]."
            expected = {
                "join_path": {
                    "left": relation_name(left),
                    "right": relation_name(right),
                    "left_column": join.from_column,
                    "right_column": join.to_column,
                    "join_type": join.join_type,
                    "reliability": join.reliability.value,
                },
                "left_key": key,
                "context_column": context_column,
                "context_value": context_value,
                "bound_column": bound_column,
                "bound_value": bound_value,
                "right_count": len(matches),
                "right_keys": [row.get(join.to_column) for row in matches],
            }
            cases.append(
                make_case(
                    f"join_{path_id}_match",
                    f"Use the signed join path to check the {relation_name(left)} record with {join.from_column} equal to {key!r} against {relation_name(right)} on {join.to_column}. {reliability_text}{context_text}{tool_hint}",
                    f"{join.join_type}_{join.reliability.value}_join",
                    expected,
                    required_tools=["benthic_join"],
                    forbidden_claims=["unsigned join", "unqualified exact match"]
                    if join.reliability.value != "reliable"
                    else ["unsigned join"],
                )
            )
            cases.append(
                make_case(
                    f"join_{path_id}_evidence",
                    f"Use the signed join path to find the relationship for {relation_name(left)} value {key!r} in {relation_name(right)}, explain the match evidence, and report whether it is reliable, partial, or heuristic. {reliability_text}{context_text}{tool_hint}",
                    f"{join.join_type}_{join.reliability.value}_join_evidence",
                    expected,
                    required_tools=["benthic_join"],
                    forbidden_claims=["unsigned join"] if join.reliability.value != "reliable" else [],
                )
            )
            join_count += 1

        coordinate = await coordinate_sample(client, catalog)
        for operation in sorted(RPC_DEFINITIONS):
            if operation == "find_district" and coordinate is not None:
                lat, lon = coordinate
                arguments = {"lat": lat, "lon": lon}
                question = f"Find the signed district containing latitude {lat} and longitude {lon}, and state the result's geographic scope."
            elif operation == "districts_in_bbox" and coordinate is not None:
                lat, lon = coordinate
                arguments = {"min_lat": lat - 0.01, "max_lat": lat + 0.01, "min_lon": lon - 0.01, "max_lon": lon + 0.01}
                question = f"List signed districts in the small bounding box around latitude {lat} and longitude {lon}; explain the limits of the result."
            elif operation == "nonprofits_nearby" and coordinate is not None:
                lat, lon = coordinate
                arguments = {"lat": lat, "lon": lon, "radius_meters": 1000}
                question = f"Find signed nonprofit records within 1000 meters of latitude {lat} and longitude {lon}; do not describe them as exact unless the returned evidence supports that."
            else:
                skipped.append({"reason": "no coordinate sample", "operation": operation})
                continue
            try:
                expected_rpc = await direct_rpc(client, catalog, operation, arguments)
            except httpx.HTTPError as exc:
                skipped.append({"reason": f"RPC unavailable: {exc}", "operation": operation})
                continue
            cases.append(
                make_case(
                    f"rpc_{operation}_rows",
                    question,
                    f"{operation}_rpc",
                    expected_rpc,
                    required_tools=["benthic_rpc"],
                    forbidden_claims=["exact geographic identity"] if operation != "find_district" else [],
                )
            )
            cases.append(
                make_case(
                    f"rpc_{operation}_limits",
                    question + " Include the operation arguments and the returned completeness/truncation information.",
                    f"{operation}_rpc_limits",
                    expected_rpc,
                    required_tools=["benthic_rpc"],
                )
            )

        queryable = [
            definition for definition in catalog.relations.values() if definition.queryable and definition.endpoint
        ]
        representative_relations = spread_across_datasets(queryable, args.discovery_cases)
        for definition in representative_relations:
            columns = choose_columns(definition, list(definition.primary_key), 3)
            cases.append(
                make_case(
                    f"discover_{safe_name(definition.dataset)}_{safe_name(definition.name)}",
                    f"Use the signed catalog to identify the relevant relation and fields in {relation_name(definition)} for a small sample of {', '.join(columns)}.",
                    "discovery",
                    {"relation": relation_name(definition), "columns": columns},
                    required_tools=["benthic_discover"],
                )
            )

        sequential_pairs: list[tuple[RelationDefinition, RelationDefinition, str]] = []
        for left in queryable:
            for right in queryable:
                if left is right:
                    continue
                shared = sorted(set(left.columns) & set(right.columns))
                shared = [column for column in shared if left.columns[column].type in {"string", "uuid", "integer"}]
                if shared:
                    sequential_pairs.append((left, right, shared[0]))
        for index, (left, right, column) in enumerate(spread_pairs(sequential_pairs, args.sequential_cases)):
            try:
                key = await sample_key(client, left, column)
                if key is None:
                    continue
                rows = await fetch_rows(
                    client, right, [column], 5, filter_params(column, key, right.columns[column].type)
                )
            except httpx.HTTPError:
                continue
            cases.append(
                make_case(
                    f"sequential_{index}_{safe_name(left.dataset)}_{safe_name(right.dataset)}",
                    f"First look up a value of {column} in {relation_name(left)} using {key!r}, then separately look up that value in {relation_name(right)}. Do not assume the two relations have a signed join.",
                    "sequential_lookup",
                    {
                        "left": relation_name(left),
                        "right": relation_name(right),
                        "column": column,
                        "key": key,
                        "right_count": len(rows),
                    },
                    required_tools=["benthic_query"],
                    forbidden_claims=["joined the relations"],
                )
            )

        signed_pairs = {
            (join.from_dataset, join.from_relation, join.to_dataset, join.to_relation) for join in catalog.joins
        }
        for index, (left, right) in enumerate(
            (queryable[i], queryable[(i + 1) % len(queryable)]) for i in range(min(3, len(queryable)))
        ):
            cases.append(
                make_case(
                    f"negative_unsigned_{index}",
                    f"Check whether the signed catalog authorizes any join between {relation_name(left)} and {relation_name(right)}. Do not inspect data or call benthic_join unless discovery returns a path. If no path exists, state that no signed path exists and stop.",
                    "unsigned_join_rejection",
                    {"must_reject": (left.dataset, left.name, right.dataset, right.name) not in signed_pairs},
                    required_tools=["benthic_discover"],
                    forbidden_claims=[],
                )
            )

        chains = two_hop_chains(catalog)
        chain_cases: list[dict[str, Any]] = []
        for chain_index, (left, middle, right, first, second) in enumerate(chains):
            for variant in range(args.multi_step_cases):
                try:
                    key = await sample_key(client, left, first.from_column)
                    if key is None:
                        skipped.append({"reason": "no sample key", "chain": f"{left.name}->{middle.name}"})
                        break
                except httpx.HTTPError:
                    break
                ask = (
                    f"Starting from the row in {relation_name(left)} with {first.from_column} {key!r}, follow the signed path to "
                    f"{relation_name(middle)} and then the signed path to {relation_name(right)}. Report the final identifier and state "
                    "each step's reliability."
                    if variant % 2 == 0
                    else f"Two signed paths connect {relation_name(left)} to {relation_name(right)} through {relation_name(middle)}. Walk both of them, "
                    f"starting from the row in {relation_name(left)} with {first.from_column} {key!r}, and report how many rows in "
                    f"{relation_name(right)} the path reaches."
                )
                chain_cases.append(
                    make_case(
                        f"multi_step_{chain_index}_{variant}_{safe_name(left.dataset)}_{safe_name(right.dataset)}",
                        ask,
                        "multi_step_join",
                        {
                            "paths": [
                                {
                                    "left": f"{first.from_dataset}.{first.from_relation}",
                                    "left_column": first.from_column,
                                    "right": f"{first.to_dataset}.{first.to_relation}",
                                    "right_column": first.to_column,
                                },
                                {
                                    "left": f"{second.from_dataset}.{second.from_relation}",
                                    "left_column": second.from_column,
                                    "right": f"{second.to_dataset}.{second.to_relation}",
                                    "right_column": second.to_column,
                                },
                            ],
                            "hub": relation_name(middle),
                            "key": key,
                        },
                        required_tools=["benthic_join"],
                        forbidden_claims=["invented join", "unsigned join"],
                    )
                )
        cases.extend(chain_cases)

        traps = relation_traps(catalog)
        trap_cases: list[dict[str, Any]] = []
        for trap_index, (correct, trap) in enumerate(traps):
            for variant in range(args.trap_cases):
                ask = (
                    f"Who held the office recorded in {relation_name(correct)} as of a past date, and what were the term "
                    "boundaries? Answer from the historical record and state the term window you used. "
                    "Do not answer from a view that only holds present-day rows."
                    if variant % 2 == 0
                    else f"List the officeholders in {relation_name(correct)} in chronological order with their term "
                    "boundaries. The present-day view is not a substitute for the historical "
                    "record."
                )
                trap_cases.append(
                    make_case(
                        f"relation_trap_{trap_index}_{variant}_{safe_name(correct.dataset)}_{safe_name(correct.name)}",
                        ask,
                        "relation_trap",
                        {"relation": relation_name(correct), "trap": relation_name(trap)},
                        required_tools=["benthic_query"],
                        forbidden_claims=[],
                    )
                )
        cases.extend(trap_cases)

        cases.sort(key=lambda case: case["id"])
        metadata = {
            "collections": sorted(snapshot.collections),
            "join_paths": len(catalog.joins),
            "rpc_operations": sorted(RPC_DEFINITIONS),
            "queryable_relations": len(queryable),
            "two_hop_chains": len(chains),
            "relation_traps": len(relation_traps(catalog)),
            "generated_cases": len(cases),
            "skipped": skipped,
        }
        return cases, metadata


def markdown(cases: list[dict[str, Any]], metadata: dict[str, Any]) -> str:
    lines = [
        "# Benthic MCP evaluation questions",
        "",
        f"Generated cases: {len(cases)}",
        f"Signed join paths: {metadata['join_paths']}",
        f"RPC operations: {', '.join(metadata['rpc_operations'])}",
        "",
    ]
    for case in cases:
        lines.extend(
            [
                f"## {case['id']}",
                f"Capability: `{case['capability']}`",
                "",
                case["question"],
                "",
            ]
        )
    if metadata["skipped"]:
        lines.extend(
            ["## Skipped generation cases", "", *[json.dumps(item, sort_keys=True) for item in metadata["skipped"]], ""]
        )
    return "\n".join(lines)


GOLDEN_DIR = ROOT / "eval" / "golden"


async def main_async(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    # Checked before any work: eval/golden is hand-verified and never regenerated, and a generator
    # run pointed at it would replace the suite that catches harness bugs with a derivative of the
    # harness.
    if output_dir.resolve() == GOLDEN_DIR.resolve():
        raise SystemExit(f"refusing to write generated cases into {GOLDEN_DIR}: those are hand-verified")
    settings = Settings.from_env()
    cases, metadata = await generate(settings, args)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "questions.json").write_text(
        json.dumps({"metadata": metadata, "cases": cases}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "questions.md").write_text(markdown(cases, metadata), encoding="utf-8")
    print(json.dumps(metadata, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "eval" / "generated"))
    parser.add_argument("--discovery-cases", type=int, default=8, help="discovery cases, spread across datasets")
    parser.add_argument("--sequential-cases", type=int, default=4, help="unsigned sequential lookup cases")
    parser.add_argument(
        "--multi-step-cases", type=int, default=2, help="question variants per two-hop signed join chain"
    )
    parser.add_argument("--trap-cases", type=int, default=2, help="question variants per present-day-view trap")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
