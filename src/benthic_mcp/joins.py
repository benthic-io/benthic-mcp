import json
from dataclasses import dataclass
from typing import Any

from benthic_mcp.catalog import Catalog
from benthic_mcp.errors import QueryValidationError
from benthic_mcp.models import (
    JoinCondition,
    JoinMetadata,
    JoinMode,
    JoinSpec,
    Reliability,
    SourceMetadata,
)
from benthic_mcp.postgrest import FetchedSource


@dataclass(frozen=True, slots=True)
class JoinResult:
    rows: list[dict[str, Any]]
    metadata: list[JoinMetadata]
    truncated: bool


def execute_joins(
    catalog: Catalog,
    fetched: list[FetchedSource],
    specs: list[JoinSpec],
    allowed_reliability: set[Reliability],
    max_rows: int,
) -> JoinResult:
    if not fetched:
        raise QueryValidationError("At least one source is required")

    rows_by_alias = {item.source.alias: [_prefix_row(item.source.alias, row) for row in item.rows] for item in fetched}
    definitions = {item.source.alias: item.definition for item in fetched}
    current_aliases = {fetched[0].source.alias}
    metadata: list[JoinMetadata] = []
    current_rows = rows_by_alias[fetched[0].source.alias]
    truncated = any(item.truncated for item in fetched)

    for spec in specs:
        left_current = spec.left_alias in current_aliases
        right_current = spec.right_alias in current_aliases
        if left_current == right_current:
            raise QueryValidationError("Each join must connect one new source alias to the accumulated result")

        if left_current:
            current_alias = spec.left_alias
            new_alias = spec.right_alias
            current_column = spec.left_column
            new_column = spec.right_column
            conditions = spec.extra_conditions
        else:
            current_alias = spec.right_alias
            new_alias = spec.left_alias
            current_column = spec.right_column
            new_column = spec.left_column
            conditions = [
                JoinCondition(left_column=condition.right_column, right_column=condition.left_column)
                for condition in spec.extra_conditions
            ]

        current_definition = definitions[current_alias]
        new_definition = definitions[new_alias]
        catalog.validate_columns(current_definition, [current_column])
        catalog.validate_columns(new_definition, [new_column])
        for condition in conditions:
            catalog.validate_columns(current_definition, [condition.left_column])
            catalog.validate_columns(new_definition, [condition.right_column])

        join = catalog.find_join(current_definition, new_definition, current_column, new_column)
        if join.join_type not in {"identifier", "heuristic"}:
            raise QueryValidationError("Spatial joins must use an allowlisted benthic_rpc operation")
        if join.reliability not in allowed_reliability:
            raise QueryValidationError(
                f"Join reliability {join.reliability.value} is not enabled. Add it to allowed_reliability to proceed."
            )
        if join.reliability.value == "partial" and not conditions:
            raise QueryValidationError(
                f"Partial signed join requires context predicates: {join.notes or 'no context note provided'}"
            )

        warnings: list[str] = []
        if join.reliability != Reliability.RELIABLE:
            message = f"Join uses {join.reliability.value} evidence: {join.notes or 'no additional note provided'}"
            warnings.append(message)
        if join.reliability == Reliability.HEURISTIC:
            warnings.append("Heuristic join matches must not be presented as exact entity matches")

        new_rows = rows_by_alias[new_alias]
        right_index: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in new_rows:
            key = _join_key(
                row,
                new_alias,
                [new_column, *(condition.right_column for condition in conditions)],
            )
            if any(value is None for value in key):
                continue
            right_index.setdefault(key, []).append(row)

        joined_rows: list[dict[str, Any]] = []
        source_truncated = False
        for row in current_rows:
            key = _join_key(
                row,
                current_alias,
                [current_column, *(condition.left_column for condition in conditions)],
            )
            matches = [] if any(value is None for value in key) else right_index.get(key, [])
            if not matches and spec.mode == JoinMode.LEFT:
                matches = [None]
            for match in matches:
                combined = dict(row)
                if match is not None:
                    combined.update(match)
                else:
                    combined.update({f"{new_alias}.{column}": None for column in new_definition.columns})
                joined_rows.append(combined)
                if len(joined_rows) > max_rows:
                    source_truncated = True
                    break
            if source_truncated:
                break

        current_rows = joined_rows
        current_aliases.add(new_alias)
        truncated = truncated or source_truncated
        metadata.append(
            JoinMetadata(
                left_alias=spec.left_alias,
                right_alias=spec.right_alias,
                left_column=spec.left_column,
                right_column=spec.right_column,
                join_type=join.join_type,
                reliability=join.reliability,
                notes=join.notes,
                warnings=warnings,
            )
        )

    return JoinResult(rows=current_rows, metadata=metadata, truncated=truncated)


def build_source_metadata(fetched: list[FetchedSource]) -> list[SourceMetadata]:
    return [
        SourceMetadata(
            alias=item.source.alias,
            source=f"{item.definition.dataset}.{item.definition.name}",
            manifest_hash=item.definition.manifest_hash,
            row_count=len(item.rows),
            complete=not item.truncated,
        )
        for item in fetched
    ]


def _prefix_row(alias: str, row: dict[str, Any]) -> dict[str, Any]:
    return {f"{alias}.{column}": value for column, value in row.items()}


def _join_key(row: dict[str, Any], alias: str, columns: list[str]) -> tuple[Any, ...]:
    values: list[Any] = []
    for column in columns:
        value = row.get(f"{alias}.{column}")
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        values.append(value)
    return tuple(values)
