import json
import math
from collections import defaultdict
from typing import Any

from benthic_mcp.bdp import BdpRepository
from benthic_mcp.catalog import Catalog
from benthic_mcp.config import Settings
from benthic_mcp.errors import QueryValidationError, UpstreamError
from benthic_mcp.joins import build_source_metadata, execute_joins
from benthic_mcp.models import (
    AggregateFunction,
    AggregateSpec,
    FilterOperator,
    FilterSpec,
    HavingSpec,
    JoinCondition,
    JoinMode,
    JoinSpec,
    OutputOrder,
    QueryRequest,
    QueryResult,
    QueryWarning,
    RelationSource,
    Reliability,
    SourceErrorPolicy,
)
from benthic_mcp.postgrest import FetchedSource, PostgrestTransport


class QueryService:
    def __init__(self, settings: Settings, repository: BdpRepository, transport: PostgrestTransport) -> None:
        self.settings = settings
        self.repository = repository
        self.transport = transport

    async def execute(self, request: QueryRequest) -> QueryResult:
        snapshot = await self.repository.load()
        catalog = Catalog(snapshot)
        fetched: list[FetchedSource] = []
        warnings = [QueryWarning(message=message) for message in snapshot.warnings]

        requires_complete = bool(request.aggregates or request.joins)
        if requires_complete:
            offset_sources = [source.alias for source in request.sources if source.offset]
            if offset_sources:
                raise QueryValidationError(
                    f"Complete aggregation and joins do not allow source offsets: {', '.join(offset_sources)}"
                )

        for source in request.sources:
            try:
                definition = catalog.resolve_relation(source.dataset, source.relation)
                source_for_fetch = source
                if not source.select:
                    required_columns = [
                        column.split(".", 1)[-1]
                        for column in [
                            *request.group_by,
                            *(item.column for item in request.aggregates if item.column),
                        ]
                        if column.split(".", 1)[-1] in definition.columns
                    ]
                    selected = _unique_strings([*required_columns, *list(definition.columns)[:3]])
                    source_for_fetch = source.model_copy(update={"select": selected})
                catalog.validate_columns(
                    definition,
                    [
                        *source_for_fetch.select,
                        *(item.column for item in source_for_fetch.filters),
                        *(item.column for item in source_for_fetch.order),
                    ],
                )
                if requires_complete:
                    fetched.append(await self.transport.fetch_complete(source_for_fetch, definition))
                else:
                    fetched.append(await self.transport.fetch(source_for_fetch, definition))
            except (QueryValidationError, UpstreamError) as exc:
                if request.on_source_error == SourceErrorPolicy.FAIL:
                    raise
                warnings.append(QueryWarning(source=source.alias, message=str(exc)))

        if not fetched:
            raise QueryValidationError("Every requested source failed")
        if len(fetched) > 1 and not request.joins:
            raise QueryValidationError("Multiple sources require at least one signed join")
        if len(fetched) < len(request.sources) and request.joins:
            raise QueryValidationError("Partial source results cannot satisfy a cross-dataset join plan")

        join_result = execute_joins(
            catalog,
            fetched,
            request.joins,
            set(request.allowed_reliability),
            self.settings.max_rows,
        )
        if requires_complete and (any(item.truncated for item in fetched) or join_result.truncated):
            raise QueryValidationError(
                "The query exceeds the complete-scan limit and cannot produce reliable aggregates or joins. "
                "Narrow the filters or raise BENTHIC_AGGREGATE_SCAN_LIMIT."
            )
        rows = _aggregate_rows(join_result.rows, request)
        available_columns = _available_source_columns(fetched)
        _validate_output_columns(request, available_columns)
        rows = _apply_having(rows, request.having)
        rows = _order_rows(rows, request, available_columns)
        columns = list(request.output_columns) if request.output_columns else _infer_columns(rows, available_columns)
        rows = [{column: row.get(column) for column in columns} for row in rows]

        final_limit = min(request.limit or self.settings.default_query_limit, self.settings.max_rows)
        page = rows[request.offset : request.offset + final_limit]
        has_more = request.offset + len(page) < len(rows)
        truncated = join_result.truncated or has_more
        for metadata in join_result.metadata:
            warnings.extend(QueryWarning(message=message) for message in metadata.warnings)

        source_complete = all(not item.truncated for item in fetched) and not join_result.truncated
        return QueryResult(
            columns=columns,
            rows=page,
            row_count=len(page),
            source_complete=source_complete,
            truncated=truncated,
            next_offset=request.offset + len(page) if has_more else None,
            sources=build_source_metadata(fetched),
            joins=join_result.metadata,
            warnings=warnings,
        )


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def build_single_query(
    *,
    question: str,
    source: str | None = None,
    dataset: str | None = None,
    relation: str | None = None,
    select: list[str] | None,
    where: list[str] | None,
    group_by: list[str] | None,
    metrics: list[str] | None,
    having: list[str] | None,
    order: list[str] | None,
    limit: int,
    offset: int,
) -> QueryRequest:
    if source is not None:
        parts = source.split(".", 1)
        if len(parts) != 2 or not all(parts):
            raise QueryValidationError("source must be dataset.relation")
        dataset, relation = parts
    if dataset is None or relation is None:
        raise QueryValidationError("A source or dataset and relation is required")

    filters = [_parse_filter(item) for item in where or []]
    aggregates = [_parse_metric(item) for item in metrics or []]
    metric_columns = [item.column for item in aggregates if item.column is not None]
    selected = _unique_strings(select or [])
    if not selected:
        selected = _unique_strings((group_by or []) + metric_columns + [item.column for item in filters])
    if not selected:
        selected = []
    selected = _unique_strings([*selected, *(item.column for item in filters), *metric_columns])
    if len(selected) > 128:
        raise QueryValidationError("A single-source query can select at most 128 columns")

    alias = "s"
    namespaced_group = [f"{alias}.{column}" for column in group_by or []]
    output_columns = (
        [*namespaced_group, *(item.alias for item in aggregates)]
        if aggregates
        else [f"{alias}.{column}" for column in selected]
    )
    output_order: list[OutputOrder] = []
    for expression in order or []:
        column, descending = _parse_order(expression)
        qualified = column if any(item.alias == column for item in aggregates) else f"{alias}.{column}"
        output_order.append(OutputOrder(column=qualified, descending=descending))

    return QueryRequest(
        question=question,
        sources=[
            RelationSource(
                alias=alias,
                dataset=dataset,
                relation=relation,
                select=selected,
                filters=filters,
            )
        ],
        output_columns=output_columns,
        group_by=namespaced_group,
        aggregates=[
            AggregateSpec(
                function=item.function,
                column=f"{alias}.{item.column}" if item.column else None,
                alias=item.alias,
            )
            for item in aggregates
        ],
        having=[_parse_having(item) for item in having or []],
        order=output_order,
        limit=limit,
        offset=offset,
    )


def build_single_join(
    *,
    question: str,
    left_source: str,
    right_source: str,
    left_column: str,
    right_column: str,
    left_where: list[str] | None = None,
    right_where: list[str] | None = None,
    left_select: list[str] | None = None,
    right_select: list[str] | None = None,
    context_conditions: list[str] | None = None,
    mode: str = "inner",
    limit: int = 100,
) -> QueryRequest:
    left_dataset, left_relation = _split_source(left_source, "left_source")
    right_dataset, right_relation = _split_source(right_source, "right_source")
    try:
        join_mode = JoinMode(mode)
    except ValueError as exc:
        raise QueryValidationError("mode must be inner or left") from exc
    left_filters = [_parse_filter(item) for item in left_where or []]
    right_filters = [_parse_filter(item) for item in right_where or []]
    if not any(item.column == right_column for item in right_filters):
        propagated = next((item for item in left_filters if item.column == left_column), None)
        if propagated is not None:
            right_filters.append(FilterSpec(column=right_column, operator=propagated.operator, value=propagated.value))
    if not any(item.column == left_column for item in left_filters):
        propagated = next((item for item in right_filters if item.column == right_column), None)
        if propagated is not None:
            left_filters.append(FilterSpec(column=left_column, operator=propagated.operator, value=propagated.value))
    extra_conditions = [_parse_context_condition(item) for item in context_conditions or []]
    # A context condition's columns have to be fetched, not just compared. A partial signed join
    # without them builds its key as (key, None), execute_joins drops every row on the null, and the
    # join reports 0 rows for a path the catalog says is populated - which is what a broken join
    # looks like, so it cannot be told apart from one.
    left_selected = _unique_strings(
        [
            *(left_select or [left_column]),
            left_column,
            *(item.column for item in left_filters),
            *(item.left_column for item in extra_conditions),
        ]
    )
    right_selected = _unique_strings(
        [
            *(right_select or [right_column]),
            right_column,
            *(item.column for item in right_filters),
            *(item.right_column for item in extra_conditions),
        ]
    )
    return QueryRequest(
        question=question,
        sources=[
            RelationSource(
                alias="left",
                dataset=left_dataset,
                relation=left_relation,
                select=left_selected,
                filters=left_filters,
            ),
            RelationSource(
                alias="right",
                dataset=right_dataset,
                relation=right_relation,
                select=right_selected,
                filters=right_filters,
            ),
        ],
        joins=[
            JoinSpec(
                left_alias="left",
                right_alias="right",
                left_column=left_column,
                right_column=right_column,
                mode=join_mode,
                extra_conditions=extra_conditions,
            )
        ],
        output_columns=[
            *(f"left.{column}" for column in left_selected),
            *(f"right.{column}" for column in right_selected),
        ],
        allowed_reliability=[Reliability.RELIABLE, Reliability.PARTIAL, Reliability.HEURISTIC],
        limit=limit,
    )


def _split_source(source: str, label: str) -> tuple[str, str]:
    parts = source.split(".", 1)
    if len(parts) != 2 or not all(parts):
        raise QueryValidationError(f"{label} must be dataset.relation")
    return parts[0], parts[1]


def _parse_context_condition(expression: str) -> JoinCondition:
    if "=" not in expression:
        raise QueryValidationError(f"Invalid context condition {expression!r}; use left_column=right_column")
    left, right = expression.split("=", 1)
    if not left.strip() or not right.strip():
        raise QueryValidationError(f"Invalid context condition {expression!r}")
    return JoinCondition(left_column=left.strip(), right_column=right.strip())


def unqualify_result(result: QueryResult) -> QueryResult:
    return result.model_copy(
        update={
            "columns": [column.split(".", 1)[-1] for column in result.columns],
            "rows": [
                {key.split(".", 1)[-1] if key.startswith("s.") else key: value for key, value in row.items()}
                for row in result.rows
            ],
        }
    )


def _parse_filter(expression: str) -> FilterSpec:
    if "=" not in expression:
        raise QueryValidationError(f"Invalid filter {expression!r}; use column=operator.value")
    column, expression_value = expression.split("=", 1)
    column = column.strip()
    if not column:
        raise QueryValidationError(f"Invalid filter {expression!r}")
    if expression_value in {FilterOperator.IS_NULL.value, FilterOperator.NOT_IS_NULL.value}:
        operator = FilterOperator(expression_value)
        return FilterSpec(column=column, operator=operator)
    if expression_value.startswith(f"{FilterOperator.IN.value}."):
        raw = expression_value.split(".", 1)[1]
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QueryValidationError(f"Invalid in filter {expression!r}; use a JSON array") from exc
        if not isinstance(values, list):
            raise QueryValidationError(f"Invalid in filter {expression!r}; use a JSON array")
        return FilterSpec(column=column, operator=FilterOperator.IN, value=values)
    if "." not in expression_value:
        raise QueryValidationError(f"Invalid filter {expression!r}; use column=operator.value")
    operator_name, value = expression_value.split(".", 1)
    try:
        operator = FilterOperator(operator_name)
    except ValueError as exc:
        raise QueryValidationError(f"Unknown filter operator {operator_name!r}") from exc
    if operator in {FilterOperator.IS_NULL, FilterOperator.NOT_IS_NULL}:
        raise QueryValidationError(f"Invalid filter {expression!r}")
    return FilterSpec(column=column, operator=operator, value=_parse_expression_value(value))


def _parse_metric(expression: str) -> AggregateSpec:
    alias = ""
    function = ""
    column = ""
    if "=" in expression:
        alias, definition = expression.split("=", 1)
        parts = definition.split(":", 1)
        if len(parts) != 2:
            raise QueryValidationError(f"Invalid metric {expression!r}; use name=function:column")
        function, column = parts
    elif ":" in expression:
        parts = expression.split(":", 2)
        if len(parts) == 3:
            alias, function, column = parts
        else:
            raise QueryValidationError(f"Invalid metric {expression!r}; use name=function:column")
    else:
        raise QueryValidationError(f"Invalid metric {expression!r}; use name=function:column")
    try:
        function_value = AggregateFunction(function)
    except ValueError as exc:
        raise QueryValidationError(f"Unknown aggregate function {function!r}") from exc
    if column == "*" and function_value != AggregateFunction.COUNT:
        raise QueryValidationError(f"{function_value.value} requires a column")
    return AggregateSpec(
        function=function_value,
        column=None if column == "*" else column,
        alias=alias.strip(),
    )


def _parse_having(expression: str) -> HavingSpec:
    if any(operator in expression for operator in (">=", "<=", "!=", ">", "<", "=")):
        separator = next(operator for operator in (">=", "<=", "!=", ">", "<", "=") if operator in expression)
        column, value = expression.split(separator, 1)
        operator = {
            ">": FilterOperator.GT,
            ">=": FilterOperator.GTE,
            "<": FilterOperator.LT,
            "<=": FilterOperator.LTE,
            "=": FilterOperator.EQ,
            "!=": FilterOperator.NEQ,
        }[separator]
    elif ":" in expression:
        parts = expression.split(":", 2)
        if len(parts) != 3:
            raise QueryValidationError(f"Invalid having expression {expression!r}")
        column, operator_name, value = parts
        try:
            operator = FilterOperator(operator_name)
        except ValueError as exc:
            raise QueryValidationError(f"Unknown having operator {operator_name!r}") from exc
    else:
        raise QueryValidationError(f"Invalid having expression {expression!r}")
    return HavingSpec(column=column.strip(), operator=operator, value=_parse_expression_value(value))


def _parse_order(expression: str) -> tuple[str, bool]:
    column, separator, direction = expression.rpartition(":")
    if not separator:
        return expression.strip(), False
    direction = direction.lower()
    if direction not in {"asc", "desc"}:
        raise QueryValidationError(f"Invalid order {expression!r}; use column:asc or column:desc")
    return column.strip(), direction == "desc"


def _parse_expression_value(value: str) -> Any:
    value = value.strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _aggregate_rows(rows: list[dict[str, Any]], request: QueryRequest) -> list[dict[str, Any]]:
    if not request.aggregates:
        return rows
    if rows and any(column not in rows[0] for column in request.group_by):
        raise QueryValidationError("A group_by column is not present in the joined result")

    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    if not rows and not request.group_by:
        groups[()] = []
    for row in rows:
        groups[tuple(row.get(column) for column in request.group_by)].append(row)

    output: list[dict[str, Any]] = []
    for key, group_rows in groups.items():
        result = dict(zip(request.group_by, key, strict=True))
        for aggregate in request.aggregates:
            if aggregate.function == AggregateFunction.COUNT:
                if aggregate.column is None:
                    result[aggregate.alias] = len(group_rows)
                else:
                    result[aggregate.alias] = sum(row.get(aggregate.column) is not None for row in group_rows)
                continue
            if aggregate.column is None:
                raise QueryValidationError(f"{aggregate.function.value} requires a column")
            values = [row.get(aggregate.column) for row in group_rows if row.get(aggregate.column) is not None]
            if aggregate.function == AggregateFunction.SUM:
                result[aggregate.alias] = sum(_numeric(value, aggregate.alias) for value in values)
            elif aggregate.function == AggregateFunction.AVG:
                numeric = [_numeric(value, aggregate.alias) for value in values]
                result[aggregate.alias] = sum(numeric) / len(numeric) if numeric else None
            elif aggregate.function == AggregateFunction.MIN:
                result[aggregate.alias] = min(values, key=_sort_value) if values else None
            else:
                result[aggregate.alias] = max(values, key=_sort_value) if values else None
        output.append(result)
    return output


def _numeric(value: Any, alias: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QueryValidationError(f"Aggregate {alias} requires numeric values")
    if isinstance(value, float) and not math.isfinite(value):
        raise QueryValidationError(f"Aggregate {alias} requires finite numeric values")
    return value


def _validate_output_columns(request: QueryRequest, source_columns: list[str]) -> None:
    available = set(source_columns)
    unknown_inputs = sorted(set(request.group_by) - available)
    unknown_inputs.extend(
        aggregate.column
        for aggregate in request.aggregates
        if aggregate.column is not None and aggregate.column not in available
    )
    if unknown_inputs:
        raise QueryValidationError(f"Unknown output columns: {', '.join(sorted(set(unknown_inputs)))}")
    available.update(aggregate.alias for aggregate in request.aggregates)
    output_references = [
        *request.output_columns,
        *(order.column for order in request.order),
        *(condition.column for condition in request.having),
    ]
    unknown_outputs = sorted(set(output_references) - available)
    if unknown_outputs:
        raise QueryValidationError(f"Unknown output columns: {', '.join(unknown_outputs)}")


def _apply_having(rows: list[dict[str, Any]], conditions: list[HavingSpec]) -> list[dict[str, Any]]:
    if not conditions:
        return rows
    return [row for row in rows if all(_matches_having(row.get(item.column), item) for item in conditions)]


def _matches_having(value: Any, condition: HavingSpec) -> bool:
    if condition.operator == FilterOperator.IS_NULL:
        return value is None
    if condition.operator == FilterOperator.NOT_IS_NULL:
        return value is not None
    if value is None:
        return False
    if condition.operator in {
        FilterOperator.GT,
        FilterOperator.GTE,
        FilterOperator.LT,
        FilterOperator.LTE,
    }:
        value = _numeric(value, condition.column)
        expected = _numeric(condition.value, condition.column)
    elif condition.operator == FilterOperator.IN:
        if not isinstance(condition.value, list):
            raise QueryValidationError("having in requires a non-empty list")
        return value in condition.value
    else:
        expected = condition.value
    comparisons = {
        FilterOperator.EQ: value == expected,
        FilterOperator.NEQ: value != expected,
        FilterOperator.GT: value > expected,
        FilterOperator.GTE: value >= expected,
        FilterOperator.LT: value < expected,
        FilterOperator.LTE: value <= expected,
        FilterOperator.LIKE: str(value) == str(expected),
        FilterOperator.ILIKE: str(value).lower() == str(expected).lower(),
    }
    result = comparisons.get(condition.operator)
    if result is None:
        raise QueryValidationError(f"Unsupported having operator {condition.operator.value}")
    return result


def _order_rows(
    rows: list[dict[str, Any]],
    request: QueryRequest,
    available_columns: list[str],
) -> list[dict[str, Any]]:
    if not request.order:
        return rows
    ordered = rows
    for order in reversed(request.order):
        ordered = sorted(
            ordered,
            key=lambda row, column=order.column: _sort_value(row.get(column)),
            reverse=order.descending,
        )
    return ordered


def _sort_value(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (1, value)
    return (1, str(value).lower())


def _available_source_columns(fetched: list[FetchedSource]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for item in fetched:
        selected = item.source.select or list(item.definition.columns)
        for column in selected:
            namespaced = f"{item.source.alias}.{column}"
            if namespaced not in seen:
                seen.add(namespaced)
                columns.append(namespaced)
    return columns


def _infer_columns(rows: list[dict[str, Any]], fallback: list[str]) -> list[str]:
    if not rows:
        return fallback
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for column in row:
            if column not in seen:
                seen.add(column)
                columns.append(column)
    return columns
