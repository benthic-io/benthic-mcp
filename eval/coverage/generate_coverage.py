"""Derive broad numeric coverage from the database and write it to eval/coverage/cases.json.

Direct PostgREST, never `benthic_query` - a number fetched through the server under test inherits
every defect the case exists to catch, which is how a suite becomes a rubber stamp. The derivation
helpers live in eval/truth/generate_numeric.py because they were proven there; this module imports
them rather than re-deriving them, and does not edit that file.

Where the truth corpus is a hand-targeted set of sharp cases, this corpus is exhaustive over the
signed manifest: every queryable relation gets at least one case, and every numeric column of every
queryable relation gets a minimum and a filtered maximum. The survey found 94 of 119 signed
relations with zero coverage; the point of this file is that a relation or column nobody thought to
target still has a case.

Five kinds, in derivation order:

- **row_count** - how many rows, bare over the whole relation or under one derived filter. A bare
  count catches a count that is refused or read over a partial scan; a filtered count catches a
  filter that silently matches nothing.
- **count_nonnull** - how many rows have a non-null X, over every number/integer column of every
  queryable relation with a primary key.
- **ordered_min** - the smallest Y in relation Z, via `order=col.asc.nullslast`. Postgres puts
  NULLS FIRST on a descending sort by default, so maxima carry `.desc.nullslast`; minima carry the
  ascending mirror and the corpus records both in `derived_from`.
- **filtered_ordered_max** - the largest Y under a filter the generator derived from the data
  rather than a target typed on a command line.
- **grouped_max** - which group has the largest total, emitted only where the relation declares a
  single-column primary key and stays under the complete-scan limit, so the aggregate can be proven
  to have read the whole relation.

Hard rules, each one a refusal rather than a compromise:

- No expected value below 10, and never 0. A count of 1 is a real answer but it is not a hard
  question, and a zero expectation is satisfied by a server that returns nothing.
- Every value is derived from the database, never hand-asserted.
- A filtered case samples its filter from the data: the first non-null value of the column at
  `order=col.asc.nullslast`, counted with `Prefer: count=exact`, and is emitted only when the count
  is at least 10 and below the relation total - a filter that matches everything is not a filter.
- Every case records the manifest hash its relation was read from, the scan limit, and how many
  seconds its derivation took. `derived_from` records the real request, including `.nullslast`.
- Tolerances: counts are exact; money figures carry `money()` only where the column name indicates
  currency (it contains one of amount, obligat, cost, value, total, balance, spend, loan); every
  other figure is exact. No blanket money().
- Every skip is recorded with its reason, in the run record under eval/coverage/runs/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval" / "truth"))

from generate_numeric import (  # noqa: E402
    case_id,
    count_all,
    count_nonnull,
    count_rows,
    grouped_max,
    money,
    ordered_max,
    relation_url,
    scan_limit,
    unreachable_because,
    whole,
)

from benthic_mcp.catalog import Catalog, RelationDefinition  # noqa: E402
from benthic_mcp.postgrest import PostgrestTransport  # noqa: E402

COVERAGE_DIR = ROOT / "eval" / "coverage"
RUNS_DIR = COVERAGE_DIR / "runs"
DEFAULT_OUT = COVERAGE_DIR / "cases.json"

# How many derivations run at once. The corpus is thousands of small requests; one at a time would
# take the better part of an hour against a public endpoint.
CONCURRENCY = 8

# Step 4's sampling rule: a filtered case may only filter on these types.
SAMPLE_TYPES = frozenset({"integer", "number", "bigint", "string", "date"})
# Minima, filtered maxima and non-null counts only ever ask about numeric columns.
NUMERIC_TYPES = frozenset({"integer", "number", "bigint"})
# Step 7: a column only gets money tolerances when its name contains one of these.
CURRENCY_HINTS = ("amount", "obligat", "cost", "value", "total", "balance", "spend", "loan")
# Preferred group columns, in order. Exact matches first, then names containing the hint.
GROUP_HINTS = (
    "state",
    "party",
    "district",
    "fiscal_year",
    "fiscal_period",
    "period",
    "year",
    "award_type",
    "entity_type",
    "funding_type",
    "category",
    "sector",
    "industry",
    "naics",
    "psc",
    "object_class",
    "code",
    "type",
    "kind",
    "chamber",
    "role",
    "committee",
    "office",
    "region",
)
# How many group columns a relation tries before giving up.
GROUP_ATTEMPTS = 2
# float64 has ~15.95 significant digits. A grouped total recorded as a float must survive the
# round-trip back to the exact decimal the case also records, so totals beyond 15 digits are
# refused rather than recorded as a float that no longer equals the audit trail.
MAX_TOTAL_DIGITS = 15

_KIND_ORDER = ("row_count", "count_nonnull", "ordered_min", "filtered_ordered_max", "grouped_max")


@dataclass(slots=True)
class Skip:
    target: str
    kind: str
    category: str
    reason: str


@dataclass(slots=True)
class Sample:
    column: str
    raw: Any
    formatted: str
    count: int


def is_currency_column(column: str) -> bool:
    return any(hint in column for hint in CURRENCY_HINTS)


def tolerance_for(column: str, value: float) -> dict[str, Any]:
    return money(value) if is_currency_column(column) else whole(value)


def display(value: Any) -> str:
    """The sample as it appears in a question: bare when identifier-safe, quoted otherwise.

    A value that reads as SQL null is quoted even though it is identifier-safe: "have
    long_description exactly NULL?" cannot say whether it means the empty cell or the text
    N-U-L-L. Measured live, 2026-10-12: naics.long_description's smallest value is the literal
    text "NULL", the literal count is 171, the SQL null count is 232, and a model answered 232
    to the unquoted question - rightly, because that is what it said.
    """
    text = str(value)
    if isinstance(value, str) and text.lower() == "null":
        return json.dumps(text, ensure_ascii=True)
    if re.fullmatch(r"[A-Za-z0-9_.:%/-]+", text):
        return text
    return json.dumps(text, ensure_ascii=True)


async def load_catalog() -> Catalog:
    """The verified catalog, built once.

    generate_numeric's `definition_for` re-verifies the signatures per target. The signature was
    verified when the snapshot was loaded; re-verifying it per case is repeated work that changes
    nothing, so this corpus builds the Catalog a single time.
    """
    from benthic_mcp.bdp import BdpRepository  # noqa: E402
    from benthic_mcp.config import Settings  # noqa: E402

    settings = Settings.from_env()
    async with httpx.AsyncClient(timeout=30.0) as client:
        snapshot = await BdpRepository(settings, client).load()
    return Catalog(snapshot)


async def guarded[T](
    sem: asyncio.Semaphore, coro: Awaitable[T], target: str, kind: str
) -> tuple[T | None, Skip | None]:
    """Run one derivation under the semaphore; a failure becomes a skip, never a crash.

    The corpus is thousands of independent derivations. One column timing out must cost that column
    its case - recorded with the reason - not the whole run.
    """
    try:
        async with sem:
            return await coro, None
    except Exception as exc:
        return None, Skip(target, kind, "request_failed", f"{type(exc).__name__}: {exc}")


async def ordered_min(client: httpx.AsyncClient, endpoint: str, column: str) -> float | None:
    """The smallest value, via SQL's own ORDER BY ascending with nulls last.

    The mirror of generate_numeric.ordered_max, for the same reason: the value must come from SQL's
    ORDER BY, so a server sorting one page in Python cannot reproduce it.
    """
    params: dict[str, str] = {"select": column, "order": f"{column}.asc.nullslast", "limit": "1"}
    response = await client.get(endpoint, params=params)
    if response.status_code >= 400:
        return None
    rows = response.json()
    if not rows:
        return None
    value = rows[0].get(column)
    return float(value) if isinstance(value, (int, float)) else None


def case_skeleton(
    definition: RelationDefinition,
    kind: str,
    column: str | None,
    value: float,
    question: str,
    derived_from: str,
    derived_at: str,
    elapsed: float,
    limit: int,
    extra: dict[str, Any],
    case_identity: str,
) -> dict[str, Any]:
    expected: dict[str, Any] = {
        "relation": f"{definition.dataset}.{definition.name}",
        "kind": kind,
        "value": value,
        # Counts are always exact, whatever the column is called: a count of a currency column is
        # a count, not a currency figure. Money tolerances are for sums and extremes only.
        **(whole(value) if kind in ("row_count", "count_nonnull") else tolerance_for(column or "", value)),
        **extra,
        "manifest_hash": definition.manifest_hash,
        "scan_limit": limit,
    }
    if column:
        expected["column"] = column
    return {
        "id": case_identity,
        "capability": "numeric_aggregate",
        "question": question,
        "expected": expected,
        "forbidden_claims": [],
        "required_tools": ["benthic_query"],
        "derived_from": derived_from,
        "derived_at": derived_at,
        "derivation_seconds": round(elapsed, 3),
    }


async def derive_totals(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    definitions: list[RelationDefinition],
    limit: int,
    derived_at: str,
    cases: list[dict[str, Any]],
    skips: list[Skip],
) -> dict[tuple[str, str], int | None]:
    """Bare row counts. Every queryable relation gets one, or a recorded reason why not.

    Each derivation appends its own skip to the shared list: the phases run under one event loop,
    so appending from a task is safe, and it keeps the guarded wrapper returning the payload alone.
    """

    async def one(definition: RelationDefinition) -> int | None:
        target = f"{definition.dataset}.{definition.name}"
        start = time.time()
        total = await count_all(client, relation_url(definition))
        if total is None:
            skips.append(Skip(target, "row_count", "no_count", "the endpoint reports no exact count"))
            return None
        if total < 10:
            skips.append(Skip(target, "row_count", "floor", f"{total} rows is below the value floor of 10"))
            return None
        cases.append(
            case_skeleton(
                definition,
                "row_count",
                None,
                float(total),
                f"How many rows are in {target}? Give the number.",
                f"HEAD {definition.name}?select=*&limit=1 with Prefer: count=exact and Range: 0-0",
                derived_at,
                time.time() - start,
                limit,
                {},
                f"cov_row_count_{definition.dataset}_{definition.name}",
            )
        )
        return total

    results = await asyncio.gather(*(guarded(sem, one(d), f"{d.dataset}.{d.name}", "row_count") for d in definitions))
    totals: dict[tuple[str, str], int | None] = {}
    for definition, (total, skip) in zip(definitions, results, strict=True):
        totals[(definition.dataset, definition.name)] = total
        if skip is not None:
            skips.append(skip)
    return totals


async def derive_samples(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    definitions: list[RelationDefinition],
    totals: dict[tuple[str, str], int | None],
    limit: int,
    derived_at: str,
    cases: list[dict[str, Any]],
    skips: list[Skip],
) -> dict[tuple[str, str], list[Sample]]:
    """Filtered row counts, one per eligible column whose sampled value matches a usable number of rows.

    The filter is derived, never typed: take the first non-null value of the column, count its
    matches with `Prefer: count=exact`, and emit only when 10 <= matches < relation total. A filter
    that matches everything is not a filter, and a value under the floor is not a hard question.
    """

    async def one(definition: RelationDefinition, column: str, total: int) -> Sample | None:
        target = f"{definition.dataset}.{definition.name}.{column}"
        url = relation_url(definition)
        start = time.time()
        probe = await client.get(url, params={"select": column, "order": f"{column}.asc.nullslast", "limit": "1"})
        if probe.status_code >= 400:
            skips.append(Skip(target, "row_count", "not_queryable", f"sample probe failed: HTTP {probe.status_code}"))
            return None
        rows = probe.json()
        if not rows:
            skips.append(Skip(target, "row_count", "no_rows", f"relation answers no rows despite counting {total}"))
            return None
        raw = rows[0].get(column)
        if raw is None:
            skips.append(
                Skip(target, "row_count", "all_null", f"{column} is all-null, so there is no value to filter on")
            )
            return None
        formatted = PostgrestTransport._format_scalar(raw)
        count = await count_rows(client, url, {column: formatted}, column)
        if count is None:
            skips.append(Skip(target, "row_count", "no_count", f"count of {column}=eq.{formatted} failed"))
            return None
        if not (10 <= count < total):
            skips.append(
                Skip(
                    target,
                    "row_count",
                    "sampling_range",
                    f"count {count} of {column}=eq.{formatted} is outside [10, {total}); "
                    "a filter that matches nothing, everything, or under the floor is not emitted",
                )
            )
            return None
        cases.append(
            case_skeleton(
                definition,
                "row_count",
                column,
                float(count),
                f"How many rows in {definition.dataset}.{definition.name} have {column} exactly "
                f"{display(raw)}? Give the number.",
                f"HEAD {definition.name}?select={column}&limit=1&{column}=eq.{formatted} "
                "with Prefer: count=exact and Range: 0-0",
                derived_at,
                time.time() - start,
                limit,
                {"sample": str(raw), "where": {column: formatted}},
                "cov_row_count_" + case_id(definition.dataset, definition.name, column, raw)[len("count_") :],
            )
        )
        return Sample(column=column, raw=raw, formatted=formatted, count=count)

    samples: dict[tuple[str, str], list[Sample]] = {}
    tasks: list[tuple[RelationDefinition, str, int]] = []
    for definition in definitions:
        total = totals[(definition.dataset, definition.name)]
        if total is None:
            skips.append(
                Skip(
                    f"{definition.dataset}.{definition.name}",
                    "row_count",
                    "no_count",
                    "no exact relation total, so a filtered count cannot be checked against it",
                )
            )
            continue
        if total < 10:
            skips.append(
                Skip(
                    f"{definition.dataset}.{definition.name}",
                    "row_count",
                    "floor",
                    f"{total} rows: no filtered count can reach the floor of 10 and stay below the relation total",
                )
            )
            continue
        tasks.extend(
            (definition, name, total) for name, column in definition.columns.items() if column.type in SAMPLE_TYPES
        )
    results = await asyncio.gather(
        *(guarded(sem, one(d, c, t), f"{d.dataset}.{d.name}.{c}", "row_count") for d, c, t in tasks)
    )
    for (definition, column, _), (sample, skip) in zip(tasks, results, strict=True):
        if sample is not None:
            samples.setdefault((definition.dataset, definition.name), []).append(sample)
        if skip is not None:
            skips.append(skip)
    return samples


async def derive_nonnull_counts(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    definitions: list[RelationDefinition],
    limit: int,
    derived_at: str,
    cases: list[dict[str, Any]],
    skips: list[Skip],
) -> None:
    """count(column) over every number/integer column of every queryable relation with a primary key.

    One HEAD with `col=not.is.null`, the same request shape the server's count pushdown sends.
    """

    async def one(definition: RelationDefinition, column: str) -> None:
        target = f"{definition.dataset}.{definition.name}.{column}"
        start = time.time()
        total = await count_nonnull(client, relation_url(definition), column)
        if total is None:
            skips.append(Skip(target, "count_nonnull", "no_count", "no non-null count"))
            return
        if total < 10:
            skips.append(
                Skip(target, "count_nonnull", "floor", f"non-null count {total} is below the value floor of 10")
            )
            return
        cases.append(
            case_skeleton(
                definition,
                "count_nonnull",
                column,
                float(total),
                f"How many rows in {definition.dataset}.{definition.name} have a non-null {column}? Give the number.",
                f"HEAD {definition.name}?select={column}&limit=1&{column}=not.is.null "
                "with Prefer: count=exact and Range: 0-0",
                derived_at,
                time.time() - start,
                limit,
                {},
                f"cov_count_nonnull_{definition.dataset}_{definition.name}_{column}",
            )
        )

    tasks = [
        (definition, name)
        for definition in definitions
        if definition.primary_key
        for name, column in definition.columns.items()
        if column.type in NUMERIC_TYPES
    ]
    results = await asyncio.gather(
        *(guarded(sem, one(d, c), f"{d.dataset}.{d.name}.{c}", "count_nonnull") for d, c in tasks)
    )
    for _, skip in results:
        if skip is not None:
            skips.append(skip)


async def derive_minima(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    definitions: list[RelationDefinition],
    limit: int,
    derived_at: str,
    cases: list[dict[str, Any]],
    skips: list[Skip],
) -> None:
    """The smallest value of every numeric column, via SQL's ascending ORDER BY with nulls last."""

    async def one(definition: RelationDefinition, column: str) -> None:
        target = f"{definition.dataset}.{definition.name}.{column}"
        start = time.time()
        value = await ordered_min(client, relation_url(definition), column)
        if value is None:
            skips.append(
                Skip(target, "ordered_min", "no_answer", "no numeric minimum: empty, all-null, or unqueryable")
            )
            return
        if value < 10:
            skips.append(Skip(target, "ordered_min", "floor", f"minimum {value} is below the value floor of 10"))
            return
        cases.append(
            case_skeleton(
                definition,
                "ordered_min",
                column,
                value,
                f"What is the smallest {column} in {definition.dataset}.{definition.name}? Give the figure.",
                f"GET {definition.name}?select={column}&order={column}.asc.nullslast&limit=1",
                derived_at,
                time.time() - start,
                limit,
                {},
                f"cov_min_{definition.dataset}_{definition.name}_{column}",
            )
        )

    tasks = [
        (definition, name)
        for definition in definitions
        for name, column in definition.columns.items()
        if column.type in NUMERIC_TYPES
    ]
    results = await asyncio.gather(
        *(guarded(sem, one(d, c), f"{d.dataset}.{d.name}.{c}", "ordered_min") for d, c in tasks)
    )
    for _, skip in results:
        if skip is not None:
            skips.append(skip)


async def derive_filtered_maxima(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    definitions: list[RelationDefinition],
    samples: dict[tuple[str, str], list[Sample]],
    limit: int,
    derived_at: str,
    cases: list[dict[str, Any]],
    skips: list[Skip],
) -> None:
    """The largest value of every numeric column under a filter the generator derived from the data.

    The filter reuses the sampling phase: the first sampled value whose count is usable. A relation
    whose columns offer no usable filter gets a recorded skip, not a hand-picked one.
    """

    async def one(definition: RelationDefinition, column: str, sample: Sample) -> None:
        target = f"{definition.dataset}.{definition.name}.{column}"
        start = time.time()
        value = await ordered_max(client, relation_url(definition), column, {sample.column: sample.formatted})
        if value is None:
            skips.append(
                Skip(target, "filtered_ordered_max", "no_answer", "no numeric maximum under the derived filter")
            )
            return
        if value < 10:
            skips.append(
                Skip(target, "filtered_ordered_max", "floor", f"maximum {value} is below the value floor of 10")
            )
            return
        cases.append(
            case_skeleton(
                definition,
                "filtered_ordered_max",
                column,
                value,
                f"What is the largest {column} in {definition.dataset}.{definition.name} over "
                f"{sample.column}={display(sample.raw)}? Give the figure.",
                f"GET {definition.name}?select={column}&order={column}.desc.nullslast&limit=1"
                f"&{sample.column}=eq.{sample.formatted}",
                derived_at,
                time.time() - start,
                limit,
                {"where": {sample.column: sample.formatted}},
                f"cov_max_{definition.dataset}_{definition.name}_{column}_{sample.column}",
            )
        )

    tasks: list[tuple[RelationDefinition, str, Sample]] = []
    for definition in definitions:
        candidates = samples.get((definition.dataset, definition.name), [])
        filter_sample = next((s for s in candidates if 10 <= s.count), None)
        if filter_sample is None:
            skips.append(
                Skip(
                    f"{definition.dataset}.{definition.name}",
                    "filtered_ordered_max",
                    "no_filter",
                    "no column produced a usable filter (10 <= matches < relation total)",
                )
            )
            continue
        for name, column in definition.columns.items():
            if column.type not in NUMERIC_TYPES:
                continue
            if name == filter_sample.column:
                skips.append(
                    Skip(
                        f"{definition.dataset}.{definition.name}.{name}",
                        "filtered_ordered_max",
                        "degenerate_filter",
                        "filter and value column coincide; the maximum would be the filter value itself",
                    )
                )
                continue
            tasks.append((definition, name, filter_sample))
    results = await asyncio.gather(
        *(guarded(sem, one(d, c, s), f"{d.dataset}.{d.name}.{c}", "filtered_ordered_max") for d, c, s in tasks)
    )
    for _, skip in results:
        if skip is not None:
            skips.append(skip)


def value_column_for(definition: RelationDefinition) -> str | None:
    """The column to sum: the first currency-named numeric column, else the first numeric column.

    Identifier-shaped numerics (id, *_id) are excluded: summing identifiers produces a figure that
    means nothing, and a model that refuses the question as meaningless would be refusing something
    the case cannot distinguish from a wrong aggregate.
    """
    key = set(definition.primary_key)
    numeric = [
        (name, column)
        for name, column in definition.columns.items()
        if column.type in NUMERIC_TYPES and name not in key and name != "id" and not name.endswith("_id")
    ]
    if not numeric:
        return None
    for name, _ in numeric:
        if is_currency_column(name):
            return name
    return numeric[0][0]


def group_candidates_for(definition: RelationDefinition, value_column: str) -> list[str]:
    """Up to GROUP_ATTEMPTS plausible group columns, preferring categorical-looking names."""
    key = set(definition.primary_key)
    scored: list[tuple[int, str]] = []
    for name, column in definition.columns.items():
        if name in key or name == value_column or column.type not in ("string", "integer"):
            continue
        if name in GROUP_HINTS:
            score = GROUP_HINTS.index(name)
        else:
            first_hit = next((i for i, hint in enumerate(GROUP_HINTS) if hint in name), len(GROUP_HINTS))
            score = len(GROUP_HINTS) + first_hit
        scored.append((score, name))
    scored.sort()
    return [name for _, name in scored[:GROUP_ATTEMPTS]]


async def derive_grouped(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    definitions: list[RelationDefinition],
    totals: dict[tuple[str, str], int | None],
    limit: int,
    derived_at: str,
    cases: list[dict[str, Any]],
    skips: list[Skip],
) -> None:
    """Which group has the largest total, only where the aggregate can be proven complete.

    The whole relation is read and summed as Decimal by generate_numeric.grouped_max. A relation is
    eligible only when it declares a single-column primary key, the exact row count is known, and
    the count stays under the complete-scan limit - otherwise the correct answer to the question is
    a refusal, which is what the truth corpus's unanswerable case grades, not what this corpus is
    for.
    """

    async def one(definition: RelationDefinition) -> None:
        target = f"{definition.dataset}.{definition.name}"
        total = totals[(definition.dataset, definition.name)]
        key = definition.primary_key
        if not key:
            skips.append(
                Skip(target, "grouped_max", "no_primary_key", "declares no primary key, so coverage cannot be verified")
            )
            return
        if len(key) > 1:
            skips.append(
                Skip(
                    target,
                    "grouped_max",
                    "composite_key",
                    f"composite key ({', '.join(key)}); the aggregate verifies one key column",
                )
            )
            return
        if total is None:
            skips.append(Skip(target, "grouped_max", "no_count", "no exact row count"))
            return
        ceiling = unreachable_because(definition, total)
        if ceiling is not None:
            skips.append(Skip(target, "grouped_max", "unreachable", ceiling))
            return
        if total < 2:
            skips.append(Skip(target, "grouped_max", "floor", "one row cannot name a winning group"))
            return
        value_column = value_column_for(definition)
        if value_column is None:
            skips.append(Skip(target, "grouped_max", "no_numeric_column", "no non-key numeric column to sum"))
            return
        for group_column in group_candidates_for(definition, value_column):
            start = time.time()
            outcome = await grouped_max(client, relation_url(definition), group_column, value_column, key=key[0])
            if outcome is None:
                skips.append(
                    Skip(
                        f"{target}.{group_column}",
                        "grouped_max",
                        "no_winner",
                        f"no winner: {value_column} is all-null, or paging disagreed with the count",
                    )
                )
                continue
            winner = outcome["total"]
            if float(winner) < 10:
                skips.append(
                    Skip(target, "grouped_max", "floor", f"winning total {winner} is below the value floor of 10")
                )
                continue
            if len(winner.as_tuple().digits) > MAX_TOTAL_DIGITS:
                skips.append(
                    Skip(
                        target,
                        "grouped_max",
                        "precision_guard",
                        f"winning total has more than {MAX_TOTAL_DIGITS} significant digits; the float recorded "
                        "as expected.value would not round-trip to the exact decimal",
                    )
                )
                continue
            cases.append(
                case_skeleton(
                    definition,
                    "grouped_max",
                    value_column,
                    float(winner),
                    f"Across every row in {target}, group by {group_column} and sum {value_column}. "
                    f"Which {group_column} has the largest total over the whole relation, and what is that total?",
                    f"GET {definition.name}?select={key[0]},{group_column},{value_column}, paged by offset until "
                    f"{outcome['rows_total']} distinct {key[0]} values were read, summed in the generator as Decimal",
                    derived_at,
                    time.time() - start,
                    limit,
                    {
                        "group_column": group_column,
                        "value_exact": str(winner),
                        "group": outcome["group"],
                        "rows_read": outcome["rows_read"],
                        "rows_total": outcome["rows_total"],
                    },
                    f"cov_group_{definition.dataset}_{definition.name}_{group_column}",
                )
            )
            return

    results = await asyncio.gather(*(guarded(sem, one(d), f"{d.dataset}.{d.name}", "grouped_max") for d in definitions))
    for _, skip in results:
        if skip is not None:
            skips.append(skip)


def results_markdown(
    cases: list[dict[str, Any]],
    skips: list[Skip],
    catalog: Catalog,
    limit: int,
    run_id: str,
    elapsed: float,
) -> str:
    kind_counts = {kind: sum(1 for c in cases if c["expected"]["kind"] == kind) for kind in _KIND_ORDER}
    relations = sorted({c["expected"]["relation"] for c in cases})
    non_queryable = sum(1 for r in catalog.relations.values() if not r.queryable)
    category_counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    for skip in skips:
        category_counts[skip.category] = category_counts.get(skip.category, 0) + 1
        examples.setdefault(skip.category, f"{skip.target}: {skip.reason}")
    category_rows = "\n".join(
        f"| {category} | {category_counts[category]} | {examples[category]} |" for category in sorted(category_counts)
    )
    kind_rows = "\n".join(f"| {kind} | {kind_counts[kind]} |" for kind in _KIND_ORDER)
    return f"""# Coverage corpus: a numeric case for every queryable relation

`eval/coverage/cases.json` is the broad counterpart of `eval/truth/numeric_cases.json`. Where the
truth corpus is a hand-targeted set of sharp cases, this corpus is exhaustive over the signed
manifest: the survey found 94 of 119 signed relations with zero coverage, and this file exists so a
relation or column nobody thought to target still has a case.

```json
{{
  "run": "{run_id}",
  "cases": {len(cases)},
  "kinds": {json.dumps(kind_counts)},
  "relations_covered": {len(relations)},
  "scan_limit": {limit},
  "skips": {len(skips)},
  "elapsed_seconds": {elapsed:.0f}
}}
```

## Conventions

- **One grader**: `eval/truth/grade.py`. These are `numeric_aggregate` cases and grade through the
  same `check_numeric` path as the truth corpus.
- **Every value is derived from the database**, through the signed PostgREST endpoint, never
  through `benthic_query`: a number fetched through the server under test inherits every defect the
  case exists to catch. The derivation helpers are imported from `eval/truth/generate_numeric.py`,
  which this corpus does not edit.
- **NULLS LAST**: Postgres puts NULLS FIRST on a descending sort by default, so every maximum is
  derived with `order={{column}}.desc.nullslast` and every minimum with
  `order={{column}}.asc.nullslast`, and every case's `derived_from` records the real request,
  including `.nullslast`.
- **Value floor**: no case expects 0 or a value under 10. A count of 1 is a real answer but not a
  hard question, and a zero expectation is satisfied by a server that returns nothing. Every skip
  the floor forces is recorded with the value that tripped it.
- **Tolerances**: counts are exact. Money figures carry `money()` only where the column name
  indicates currency - it contains one of amount, obligat, cost, value, total, balance, spend,
  loan. Every other figure is exact. There is no blanket money().
- **Filters are derived, never typed**: a filtered case takes the first non-null value of its
  column at `order={{column}}.asc.nullslast`, counts matches with `Prefer: count=exact`, and is
  emitted only when the count is at least 10 and below the relation total.
- **Provenance per case**: every case records `manifest_hash` (the signed manifest its relation was
  read from), `scan_limit`, and `derivation_seconds`, next to `derived_from` and `derived_at`.

## Kinds

| kind | cases |
| --- | --- |
{kind_rows}

## Skips

| category | count | example |
| --- | --- | --- |
{category_rows}

The full skip list, one reason per target, is in `eval/coverage/runs/{run_id}.json` (gitignored,
like every other run evidence directory).

## What is not measured here

- **Relations that declare no primary key never get a grouped_max case** - the correct answer to
  an unverifiable aggregate is a refusal, which the truth corpus already grades as a decline.
- **Non-queryable relations are out of scope entirely**: {non_queryable} of the 119 signed
  relations declare `queryable: false` and get no case at all.
- **A filtered case only exercises one sample per column.** The filter is the first non-null value
  at ascending order; other values of the same column are not separately counted.
- **Spatial joins are not exercised here.** They go through benthic_rpc, not PostgREST, so a
  numeric corpus cannot reach them.
- **Counts are single figures, not distributions.** Whether the model reaches the figure by
  counting exactly or by reading the server's own count is not distinguished by the grader.
"""


async def run(output: Path) -> None:
    catalog = await load_catalog()
    limit = scan_limit()
    started = time.time()
    run_id = time.strftime("run-%Y%m%dT%H%M%S")
    derived_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    cases: list[dict[str, Any]] = []
    skips: list[Skip] = []
    definitions = sorted(
        (d for d in catalog.relations.values() if d.queryable and d.endpoint is not None),
        key=lambda d: (d.dataset, d.name),
    )
    sem = asyncio.Semaphore(CONCURRENCY)
    # 45s per request: the api_user role kills any statement at 30s, so a request that outlives 45s
    # is never going to answer, and waiting for it holds up every derivation queued behind it.
    async with httpx.AsyncClient(timeout=45.0) as client:
        print(f"coverage: {len(definitions)} queryable relations, scan limit {limit:,}", flush=True)
        print("  row_count...", flush=True)
        totals = await derive_totals(client, sem, definitions, limit, derived_at, cases, skips)
        print("  sampled filters...", flush=True)
        samples = await derive_samples(client, sem, definitions, totals, limit, derived_at, cases, skips)
        print(f"  sampled filters: {sum(len(v) for v in samples.values())} usable", flush=True)
        print("  count_nonnull...", flush=True)
        await derive_nonnull_counts(client, sem, definitions, limit, derived_at, cases, skips)
        print("  ordered_min...", flush=True)
        await derive_minima(client, sem, definitions, limit, derived_at, cases, skips)
        print("  filtered_ordered_max...", flush=True)
        await derive_filtered_maxima(client, sem, definitions, samples, limit, derived_at, cases, skips)
        print("  grouped_max...", flush=True)
        await derive_grouped(client, sem, definitions, totals, limit, derived_at, cases, skips)

    cases.sort(key=lambda c: (_KIND_ORDER.index(c["expected"]["kind"]), c["id"]))
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise SystemExit(f"refusing to write a corpus with duplicate case ids: {duplicates}")

    COVERAGE_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    document = {
        "metadata": {
            "generated": time.strftime("%Y-%m-%d"),
            "cases": len(cases),
            "kinds": {kind: sum(1 for c in cases if c["expected"]["kind"] == kind) for kind in _KIND_ORDER},
            "relations_covered": len({c["expected"]["relation"] for c in cases}),
            "scan_limit": limit,
            "skips": len(skips),
            "skip_categories": sorted({s.category for s in skips}),
        },
        "cases": cases,
    }
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    (RUNS_DIR / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run": run_id,
                "cases": len(cases),
                "scan_limit": limit,
                "concurrency": CONCURRENCY,
                "elapsed_seconds": round(time.time() - started, 1),
                "skips": [
                    {"target": s.target, "kind": s.kind, "category": s.category, "reason": s.reason} for s in skips
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (COVERAGE_DIR / "RESULTS.md").write_text(
        results_markdown(cases, skips, catalog, limit, run_id, time.time() - started), encoding="utf-8"
    )
    for kind in _KIND_ORDER:
        count = sum(1 for c in cases if c["expected"]["kind"] == kind)
        skipped = sum(1 for s in skips if s.kind == kind)
        print(f"  {kind}: {count} cases, {skipped} skips", flush=True)
    print(f"\n  {len(cases)} coverage cases -> {output} ({time.time() - started:.0f}s, {len(skips)} skips)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    # Checked before any network work: the corpus has exactly one home and one name, because the
    # contracts in tests/test_coverage_corpus_location.py assert both. A generator that could be
    # pointed at a scratch path with a scratch name would quietly produce a corpus those contracts
    # never see.
    out = Path(args.output).resolve()
    if out.parent != COVERAGE_DIR.resolve():
        raise SystemExit(f"refusing to write coverage cases to {out.parent}: the corpus lives at {COVERAGE_DIR}")
    if out.name != "cases.json":
        raise SystemExit(f"refusing to write coverage cases to {out.name}: the corpus file is cases.json")
    asyncio.run(run(out))


if __name__ == "__main__":
    main()
