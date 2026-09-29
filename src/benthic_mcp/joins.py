import json
from dataclasses import dataclass
from typing import Any

from benthic_mcp.catalog import Catalog, RelationDefinition
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
        # Keyed on join_type as well as reliability, because the two are separate declarations and a
        # collection can grade a fuzzy edge as reliable. Reliability alone let a heuristic-typed edge
        # return no warning at all, which makes a fuzzy match indistinguishable from an exact one.
        if join.join_type == "heuristic" and join.reliability == Reliability.RELIABLE:
            warnings.append(
                "This signed edge is graded reliable but declared as a heuristic match, so the grade "
                "and the join type disagree; the result is not an exact identifier match"
            )

        new_rows = rows_by_alias[new_alias]
        coercion = _type_coercion(current_definition, current_column, new_definition, new_column)
        if coercion is not None:
            warnings.append(coercion)
        right_index: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in new_rows:
            key = _join_key(
                row,
                new_alias,
                [new_column, *(condition.right_column for condition in conditions)],
                coerce=coercion is not None,
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
                coerce=coercion is not None,
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


def _type_coercion(
    left_definition: RelationDefinition,
    left_column: str,
    right_definition: RelationDefinition,
    right_column: str,
) -> str | None:
    """A note when the two sides of a signed join are declared with different scalar types.

    Keys are compared in Python, not in SQL, so nothing coerces them. A signed edge between
    `usaspending.all_entities.congressional_district` (text, zero-padded, `'03'`) and
    `usp_cl.legislator_terms.district` (integer, `3`) therefore matched nothing, on every call, for
    as long as the suite had been running. The scores said the join was being taken correctly,
    because the scorer only ever checked the route.

    Returns the warning to attach, and the presence of a warning is what switches key comparison to
    the normalised form. Only integer-versus-text is coerced, and only by stripping leading zeros,
    so `'03'` and `3` agree while `'abc'` and `0` do not.
    """
    left = left_definition.columns.get(left_column)
    right = right_definition.columns.get(right_column)
    if left is None or right is None or left.type == right.type:
        return None
    # `number` is the catalog's own name for a decimal column, not a PostgREST type, and it is the
    # second most common declaration in the signed manifest: 427 of 3419 columns. Leaving it out
    # meant a text key against a number key still matched nothing, which is the same defect this
    # function exists to prevent, one type name over.
    text_like = {"string", "varchar", "text", "character varying"}
    numeric = {"integer", "bigint", "smallint", "numeric", "number", "real", "double precision", "decimal"}
    mismatch = (left.type in text_like and right.type in numeric) or (right.type in text_like and left.type in numeric)
    if not mismatch:
        return None
    return (
        f"Join keys are declared as different types ({left.type} against {right.type}); matching on the "
        f"zero-stripped form, so {left_column}='03' will match {right_column}=3"
    )


def _coerce_token(value: Any) -> Any:
    """One comparable token for a key on a join whose sides are declared with different types.

    Both sides are reduced to the same form, because normalising only one leaves '3' against 3.
    A string is stripped of leading zeros, an integral number is rendered without its decimal
    point, and anything else is left alone so a word still cannot equal a number.
    """
    if isinstance(value, str):
        stripped = value.lstrip("0")
        return stripped or ("0" if value else value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return value


def _join_key(row: dict[str, Any], alias: str, columns: list[str], coerce: bool = False) -> tuple[Any, ...]:
    values: list[Any] = []
    for column in columns:
        value = row.get(f"{alias}.{column}")
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        elif coerce:
            value = _coerce_token(value)
        values.append(value)
    return tuple(values)
