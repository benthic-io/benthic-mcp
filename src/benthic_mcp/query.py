import json
import math
import re
from collections import defaultdict
from typing import Any

from benthic_mcp.bdp import BdpRepository
from benthic_mcp.catalog import Catalog, RelationDefinition
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
    SourceMetadata,
    SourceOrder,
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

        if requires_complete:
            counted = await self._server_count(request, catalog, warnings)
            if counted is not None:
                return counted

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
        by_alias = {item.source.alias: item.definition for item in fetched}
        if requires_complete and (any(item.truncated for item in fetched) or join_result.truncated):
            raise QueryValidationError(
                _scan_refusal(
                    fetched,
                    self.settings.aggregate_scan_limit,
                    self.settings.max_rows,
                    request=request,
                )
            )
        rows = _aggregate_rows(join_result.rows, request)
        available_columns = _available_source_columns(fetched)
        _validate_output_columns(request, available_columns, catalog, by_alias)
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
        if truncated:
            warnings.extend(_truncation_warnings(fetched))
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

    async def _server_count(
        self,
        request: QueryRequest,
        catalog: Catalog,
        warnings: list[QueryWarning],
    ) -> QueryResult | None:
        """Answer `count(*)` from the count HEAD, for the one shape that always fits.

        A bare count has exactly one output row however wide the source is, so the complete-scan
        limit does not apply to it. It was being refused anyway: `count(*)` on
        usaspending.reporting_agency_overview was refused at 10,545 rows against a 10,000 limit,
        and on usaspending.all_entities filtered to district 03 at 1,416,153. Neither has a filter
        that makes it fit, so both were dead ends, and a model with a turn budget spends the rest of
        it re-issuing the same call.

        PostgREST answers it without a scan. Bare `select=count` is not gated by this server's
        db-aggregates-enabled setting - every aggregate-function spelling returns PGRST123 - and the
        count HEAD this path already sends carries the exact total in Content-Range. Verified
        live: 1,416,153 for the district filter in 1.23s against a 17,884,243-row table.

        Deliberately narrow. A group_by, a second aggregate, a `count(column)`, a join, or an order
        on anything other than the alias all fall through to the scan, because there the output
        size depends on the data rather than on the source width and the count cannot answer it.
        None means "not this shape" or "the count did not come back" - never a number.
        """
        if request.joins or request.group_by or len(request.sources) != 1 or len(request.aggregates) != 1:
            return None
        only = request.aggregates[0]
        if only.function != AggregateFunction.COUNT or only.column is not None:
            return None
        if request.having or any(item.column != only.alias for item in request.order):
            return None

        source = request.sources[0]
        try:
            definition = catalog.resolve_relation(source.dataset, source.relation)
            catalog.validate_columns(
                definition,
                [*(item.column for item in source.filters), *(item.column for item in source.order)],
            )
            count = await self.transport.count_matching(source, definition)
        except (QueryValidationError, UpstreamError):
            return None
        if count is None:
            return None

        rows = _apply_having([{only.alias: count.total}], request.having)
        rows = _order_rows(rows, request, [only.alias])
        columns = [only.alias]
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            # The count is exact by construction, which is the whole point: it did not come from a
            # scan that ran out of budget.
            source_complete=True,
            truncated=False,
            next_offset=None,
            sources=[
                SourceMetadata(
                    alias=source.alias,
                    source=f"{definition.dataset}.{definition.name}",
                    manifest_hash=definition.manifest_hash,
                    row_count=count.total,
                    complete=True,
                )
            ],
            joins=[],
            warnings=warnings,
        )


def _truncation_warnings(fetched: list[FetchedSource]) -> list[QueryWarning]:
    """Say how much is missing, and what that means for the answer, on every truncated result.

    Found by driving the live interface. A session asking for entities in MA district 03 got 100 of
    1,416,153 rows, saw only `truncated: true`, and spent every remaining turn querying again. The
    server knew the exact size - it had just paid for a HEAD to find out - and discarded it on the
    path that succeeded, after using it only to build refusal text.

    The stop rule was already in the tool description the model reads on every turn. It did not
    work, because the model had no way to judge whether one more call could help. A count gives it
    that: at 1.4 million rows no amount of further paging produces an answer, and at 8 rows out of 8
    the result is already complete. Without the number both cases look identical.

    A truncated source whose size is unknown says so rather than implying it is knowable.
    """
    warnings: list[QueryWarning] = []
    for item in fetched:
        if not item.truncated:
            continue
        name = f"{item.definition.dataset}.{item.definition.name}"
        matched = item.matched_rows
        if matched is None:
            warnings.append(
                QueryWarning(
                    source=item.source.alias,
                    message=(
                        f"{name} returned {len(item.rows)} rows and the result was cut off. "
                        f"How many rows match the filters could not be established, so it is not "
                        f"known whether further calls would reach them."
                    ),
                )
            )
            continue
        if matched <= len(item.rows):
            warnings.append(
                QueryWarning(
                    source=item.source.alias,
                    message=(
                        f"{name} returned all {matched} rows that match the filters. The source is "
                        f"complete and no further call will return anything new."
                    ),
                )
            )
            continue
        warnings.append(
            QueryWarning(
                source=item.source.alias,
                message=(
                    f"{name} returned {len(item.rows)} of {matched} rows that match the filters. "
                    f"Paging cannot produce an answer at this size, so answer now with the rows held "
                    f"and state that the result is a partial view rather than a total."
                ),
            )
        )
    return warnings


def _count_is_the_answer(request: QueryRequest, widest: FetchedSource) -> tuple[bool, str | None]:
    """Whether the matching-row count answers this request outright, and why not when it does not.

    A count-only request with nothing grouped wants one number, and the count the refusal already
    carries is that number. Measured on `truncation`: asked for the count of organisations in
    Massachusetts, the model was refused and told to narrow, then spent twelve turns enumerating
    subsection, foundation and affiliation codes to arrive at a number it had already been given. Over
    121 cap-then-success retries it narrowed 121 times and repeated the request zero times, so it is
    not failing to understand "narrow" - it is being sent to narrow when narrowing cannot be what the
    question wants.

    `count(some_column)` is a different number whenever that column is nullable, so the count is only
    offered as the answer for count(*) and for columns the manifest marks non-nullable. Otherwise it
    is an upper bound, and the caller is told the one filter that makes the two numbers equal.
    """
    if request.group_by or request.having or request.joins:
        return False, None
    if len(request.sources) > 1:
        return False, None
    if not request.aggregates or any(aggregate.function != AggregateFunction.COUNT for aggregate in request.aggregates):
        return False, None

    columns = widest.definition.columns

    # The tool layer qualifies a column with its source alias, so a count arrives as `s.id` while
    # the definition holds bare `id`. Without stripping the prefix the lookup finds nothing, treats
    # the column as unknown, and declines to make any claim - which is how the branch was inert on
    # the live server while every direct call to it returned the right answer.
    named = [aggregate.column.split(".", 1)[-1] for aggregate in request.aggregates if aggregate.column]
    nullable = [name for name in named if name in columns and columns[name].nullable]
    unknown = [name for name in named if name not in columns]
    if nullable:
        names = ", ".join(f"`{name}`" for name in nullable)
        return False, names
    if unknown:
        return False, None
    return True, None


def _scan_exit(definition: RelationDefinition, scan_limit: int) -> str:
    """The way out of a scan refusal, which is not the same exit for every relation.

    A relation declaring no primary key cannot be paged deterministically, so `fetch_complete` refuses
    an aggregate over it once more than one page of rows matches - a rule separate from the
    complete-scan cap, and one that narrowing below the cap does not escape. Measured on
    `usaspending.reporting_agency_overview`: 10,545 rows is refused by the scan cap, and 1,221 - well
    under it - is still refused for the missing primary key. Telling that caller to narrow below the
    cap is a dead end, and it is the dead end `query_having_text` sat in for 35 cycles.
    """
    if not definition.primary_key:
        return (
            f"{definition.dataset}.{definition.name} declares no primary key, so its rows cannot be "
            "paged deterministically and an aggregate over them is refused however far you narrow. "
            "Getting under the scan limit will not help, and no sequence of queries reaches a "
            "cross-row total here: a per-period or per-value total is a different question and must "
            "not be summed into one. Report that the aggregate cannot be answered rather than "
            "enumerating."
        )
    return (
        f"Narrow the filters until each source matches at most {scan_limit} rows, or raise "
        "BENTHIC_AGGREGATE_SCAN_LIMIT."
    )


def _scan_refusal(
    fetched: list[FetchedSource], scan_limit: int, max_rows: int, request: QueryRequest | None = None
) -> str:
    """Refuses a complete scan that cannot be made exact, naming what put it over.

    "Narrow the filters" is not an instruction a caller can act on. A filter already narrowed to a
    single congressional district is told exactly what an unfiltered scan is told, and has no way to
    see whether it is 10 percent or 1400 percent over, so it can neither tighten the filter nor
    conclude that filtering cannot help and a pre-aggregated relation is the way.

    Where the count the refusal already carries is itself the answer, it says so, because telling a
    caller to narrow towards a number it is already holding is the instruction that spent twelve turns
    on `truncation`.
    """
    over = [item for item in fetched if item.truncated]
    if not over:
        return (
            f"Every source was scanned in full, so the join itself exceeds the row limit of {max_rows}. "
            f"Raise BENTHIC_MAX_ROWS, join fewer relations at a time, or aggregate one source at a time."
        )

    widest = max(over, key=lambda item: item.matched_rows or 0)
    source_name = f"{widest.definition.dataset}.{widest.definition.name}"
    exit_advice = _scan_exit(widest.definition, scan_limit)
    if widest.matched_rows is None:
        # The count did not come back, so the cap is the only size known. Naming it is still worth
        # more than silence: the caller can see that a gap exists even if not how wide it is.
        magnitude = (
            f"More than {scan_limit} rows match the filters in {source_name}, "
            f"past the complete-scan limit of {scan_limit}"
        )
        return f"{magnitude}. Aggregating or joining needs every source scanned in full. {exit_advice}"

    answered, nullable = (False, None)
    if request is not None:
        answered, nullable = _count_is_the_answer(request, widest)

    if answered:
        return (
            f"{widest.matched_rows} rows match the filters in {source_name}, which is "
            f"{widest.matched_rows - scan_limit} more than the complete-scan limit of {scan_limit}. "
            f"You asked only how many rows match and nothing is grouped, so {widest.matched_rows} is "
            f"the answer to the question as asked. Report it as exact rather than narrowing."
        )

    magnitude = (
        f"{widest.matched_rows} rows match the filters in {source_name}, "
        f"{widest.matched_rows - scan_limit} more than the complete-scan limit of {scan_limit}"
    )
    if nullable:
        # An upper bound is not the answer, and saying so plainly is what keeps the caller from
        # reporting it as one. Excluding the nulls is the narrowing that makes the two equal.
        magnitude += (
            f", which is an upper bound on the count of {nullable} because that column is nullable. "
            f"Adding {nullable}=not.is.null makes the row count and the column count the same number, "
            f"at the cost of a narrower filter."
        )
    others = f", the widest of the {len(over)} sources over it" if len(over) > 1 else ""
    return f"{magnitude}{others}. Aggregating or joining needs every source scanned in full. {exit_advice}"


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
    source_order: list[SourceOrder] = []
    # Only a plain row select can hand its ordering to the database. An aggregate alias is computed
    # output and a grouped query orders over the group, so neither has a column to push down; both
    # stay sorted by `_order_rows` afterwards, which is correct there because the database computed them.
    pushable = not aggregates and not group_by
    for expression in order or []:
        column, descending = _parse_order(expression)
        on_aggregate = any(item.alias == column for item in aggregates)
        qualified = column if on_aggregate else f"{alias}.{column}"
        output_order.append(OutputOrder(column=qualified, descending=descending))
        if pushable and not on_aggregate:
            source_order.append(SourceOrder(column=column, descending=descending))

    return QueryRequest(
        question=question,
        sources=[
            RelationSource(
                alias=alias,
                dataset=dataset,
                relation=relation,
                select=selected,
                filters=filters,
                order=source_order,
                limit=limit,
                offset=offset,
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


_NULL_TESTS = (FilterOperator.IS_NULL, FilterOperator.NOT_IS_NULL)

_FILTER_OPERATORS = ", ".join(operator.value for operator in FilterOperator if operator not in _NULL_TESTS)
_FILTER_SYNTAX = (
    "use 'column=operator.value'. Example: 'congressional_district=eq.03'. "
    f"Operators are {_FILTER_OPERATORS}. Values are unquoted; "
    "'column=in.[\"a\",\"b\"]' takes a JSON array, and 'column=is.null' or 'column=not.is.null' takes no value. "
    "'column>0' is also accepted for the comparison operators."
)


def _invalid_filter(expression: str, reason: str = "") -> QueryValidationError:
    detail = f" {reason}" if reason else ""
    return QueryValidationError(f"Invalid filter {expression!r};{detail} {_FILTER_SYNTAX}")


def _parse_filter(expression: str) -> FilterSpec:
    # `column>0` and `column<0` carry the operator without the dot, which is the next thing a caller
    # writes after `column=value` and is what appeared twice in the live transcripts. Unlike a bare
    # `column=value` there is no ambiguity - nothing else reads as greater-than - so it is mapped
    # rather than refused.
    comparison = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*(>=|<=|>|<)\s*(.+)$", expression)
    if comparison:
        column, symbol, value = comparison.groups()
        operator = {
            ">": FilterOperator.GT,
            ">=": FilterOperator.GTE,
            "<": FilterOperator.LT,
            "<=": FilterOperator.LTE,
        }[symbol]
        return FilterSpec(column=column.strip(), operator=operator, value=_parse_expression_value(value.strip()))
    if "=" not in expression:
        raise _invalid_filter(expression)
    column, expression_value = expression.split("=", 1)
    column = column.strip()
    if not column:
        raise _invalid_filter(expression, "there is no column before the '='.")
    if expression_value.lower() in {FilterOperator.IS_NULL.value, FilterOperator.NOT_IS_NULL.value}:
        operator = FilterOperator(expression_value.lower())
        return FilterSpec(column=column, operator=operator)
    if expression_value.startswith(f"{FilterOperator.IN.value}."):
        raw = expression_value.split(".", 1)[1]
        # The worked example is built from the caller's own column, because the refusal used to say
        # only "use a JSON array" and a model that had copied the syntax text's own example got that
        # message and no spelling that worked. See test_no_filter_example_the_server_advertises_is_one_it_would_refuse.
        example = f'{column}=in.["A","B"]'
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QueryValidationError(
                f"Invalid in filter {expression!r}; use a JSON array, for example {example!r}"
            ) from exc
        if not isinstance(values, list):
            raise QueryValidationError(f"Invalid in filter {expression!r}; use a JSON array, for example {example!r}")
        return FilterSpec(column=column, operator=FilterOperator.IN, value=values)
    if "." not in expression_value:
        # Two shapes reach here, and they need different messages.
        if ":" in expression_value:
            # `column=eq:value` names its operator as plainly as `column=eq.value`; only the
            # separator differs. Live transcripts show a model writing `district=eq:3` and
            # `congressional_district=like:%MA-03%`, each refused with a message claiming no
            # operator was present. There is exactly one reading, so it is accepted.
            prefix, _, rest = expression_value.partition(":")
            known = {item.value for item in FilterOperator}
            if prefix.lower() in known:
                operator = FilterOperator(prefix.lower())
                if operator not in _NULL_TESTS:
                    return FilterSpec(column=column, operator=operator, value=_parse_expression_value(rest))
                raise _invalid_filter(expression, f"{operator.value!r} takes no value, so nothing may follow it.")
            raise _invalid_filter(
                expression,
                f"{prefix!r} is not an operator; write 'column=operator.value' with a dot.",
            )
        # `column=value` names nothing at all, which is different: it could be a dropped operator or
        # a dropped value and there is nothing to go on. Refused, with the reason named.
        raise _invalid_filter(
            expression,
            f"{expression_value!r} is a value, not an operator, so it has no operator before it.",
        )

    operator_name, value = expression_value.split(".", 1)
    try:
        # Case-insensitive for the same reason aggregate functions are: the message lists only
        # lowercase, and a caller who reads it and re-sends the spelling it was shown must not fail.
        operator = FilterOperator(operator_name.lower())
    except ValueError as exc:
        # `is.null` and `not.is.null` are the two operators that contain a dot, so a value appended
        # to one arrives here with the null test already split in half. Name the whole thing rather
        # than the fragment, which is what the caller actually wrote.
        prefixed = f"{operator_name}.{value}"
        for null_test in _NULL_TESTS:
            if prefixed.startswith(null_test.value):
                raise _invalid_filter(
                    expression,
                    f"{null_test.value!r} takes no value, so nothing may follow it.",
                ) from exc
        raise _invalid_filter(
            expression,
            f"{operator_name!r} is not an operator.",
        ) from exc
    if operator in {FilterOperator.IS_NULL, FilterOperator.NOT_IS_NULL}:
        # These take no value, so `column=is.null.something` cannot mean anything. Saying only
        # "Invalid filter" left the model guessing at the shape; it was passing a value.
        raise _invalid_filter(
            expression,
            f"{operator.value!r} takes no value, so nothing may follow it.",
        )
    return FilterSpec(column=column, operator=operator, value=_parse_expression_value(value))


# Derived from the enums so a name can never be listed here that the parser does not accept.
_AGGREGATE_FUNCTIONS = ", ".join(function.value for function in AggregateFunction)

# is.null and not.is.null are absent on purpose: HavingSpec refuses a value for them and the ':'
# form always carries one, so in a having they can only fail.
_HAVING_OPERATORS = ", ".join(
    operator.value
    for operator in FilterOperator
    if operator not in {FilterOperator.IS_NULL, FilterOperator.NOT_IS_NULL}
)

# One wording for every rejected metric, so whichever shape a caller got wrong, the message names
# the accepted forms, gives a working example, and says which of the two columns the name is. It
# said "name=function:column" while the tool docstring said "alias=function:column" for the same
# slot, and a caller read "name" as the source column, retried with it on the left, and got a valid
# result under a new output name - so the correction it had made was never confirmed.
_METRIC_SYNTAX = (
    "use '<output_name>=<function>:<column>', e.g. 'total_sum=sum:total_obligation', or "
    "'<output_name>:<function>:<column>' with the same three parts. The name before the first "
    "separator is the OUTPUT column the aggregate is returned under, not the source column. "
    f"Functions are {_AGGREGATE_FUNCTIONS}; '<output_name>=count:*' counts rows."
)

_MISSING_METRIC_NAME = "The output name is missing: it goes before the first '=' or ':'."
_COLON_IN_COLUMN = "A column cannot contain ':'."
_MISSING_METRIC_COLUMN = "The source column is empty."

_HAVING_SYNTAX = (
    "use '<output_name><op><value>' with <op> one of >=, <=, !=, >, <, =, e.g. 'total_sum>100000', "
    "or '<output_name>:<operator>:<value>', e.g. 'total_sum:gt:100000'. The name is a metrics "
    f"output_name or a group_by column, not a source column. ':' operators are {_HAVING_OPERATORS}."
)


def _invalid_syntax(expression: str, kind: str, syntax: str, reason: str = "") -> QueryValidationError:
    """Build the error a malformed expression gets, with the reason only when there is a specific one.

    Every rejected shape of one slot has to end here, or a caller that hit an untested shape gets a
    bare restatement of the form and spends a turn on it.
    """
    message = f"Invalid {kind} {expression!r}; {syntax}"
    if reason:
        message = f"{message} {reason}"
    return QueryValidationError(message)


def _parse_metric(expression: str) -> AggregateSpec:
    # Every split is uncapped, so an extra ':' is seen here. The capped split took 'total:sum:col:extra'
    # as a column of 'col:extra', which the caller only heard about once the fetched relation turned
    # out to have no such column, long after the metric was built.
    if "=" in expression:
        alias, definition = expression.split("=", 1)
        parts = definition.split(":")
        if len(parts) != 2:
            raise _invalid_syntax(expression, "metric", _METRIC_SYNTAX, _COLON_IN_COLUMN if len(parts) > 2 else "")
        function, column = parts
    else:
        parts = expression.split(":")
        if len(parts) != 3:
            # Two parts is a bare 'function:column', which has no slot for an output name at all.
            reason = _MISSING_METRIC_NAME if len(parts) == 2 else _COLON_IN_COLUMN
            raise _invalid_syntax(expression, "metric", _METRIC_SYNTAX, reason)
        alias, function, column = parts
    # Every part is stripped: only the name was, so 'n = sum : col' read as an unknown function.
    alias = alias.strip()
    function = function.strip()
    column = column.strip()
    if not alias:
        # Caught here rather than by AggregateSpec, whose pattern failure is a pydantic message
        # rather than one naming the form.
        raise _invalid_syntax(expression, "metric", _METRIC_SYNTAX, _MISSING_METRIC_NAME)
    if not column:
        raise _invalid_syntax(expression, "metric", _METRIC_SYNTAX, _MISSING_METRIC_COLUMN)
    try:
        # Case-insensitive on purpose. The error listed only lowercase spellings, and a model read it,
        # concluded "the function should be lowercase", and then sent the uppercase spelling anyway -
        # twice, on consecutive turns, with the same wrong call. Accepting `MAX` costs nothing and
        # ends that loop; the function name is not something a caller should have to case-match.
        function_value = AggregateFunction(function.lower())
    except ValueError as exc:
        raise QueryValidationError(f"Unknown aggregate function {function!r}; use {_AGGREGATE_FUNCTIONS}") from exc
    if column == "*" and function_value != AggregateFunction.COUNT:
        raise QueryValidationError(f"{function_value.value} requires a column")
    return AggregateSpec(
        function=function_value,
        column=None if column == "*" else column,
        alias=alias,
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
            raise _invalid_syntax(expression, "having expression", _HAVING_SYNTAX)
        column, operator_name, value = parts
        try:
            operator = FilterOperator(operator_name)
        except ValueError as exc:
            raise QueryValidationError(f"Unknown having operator {operator_name!r}; use {_HAVING_OPERATORS}") from exc
    else:
        raise _invalid_syntax(expression, "having expression", _HAVING_SYNTAX)
    column = column.strip()
    if not column:
        # Same reason as an empty metric name: HavingSpec would refuse it with the pydantic pattern
        # text, which names neither the form nor an example.
        raise _invalid_syntax(expression, "having expression", _HAVING_SYNTAX, "The compared name is empty.")
    return HavingSpec(column=column, operator=operator, value=_parse_expression_value(value))


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


def _validate_output_columns(
    request: QueryRequest,
    source_columns: list[str],
    catalog: Catalog | None = None,
    definitions: dict[str, RelationDefinition] | None = None,
) -> None:
    """Reject output positions naming a column that was not fetched, naming near misses.

    A wrong guess is recoverable in select, in a filter and in a source order, because
    catalog.validate_columns offers the signed near misses. It was a dead end here, in the
    positions the model uses to say what it wants back: group_by, aggregates, having,
    output_columns and order. Candidates come from the signed catalog and from nothing else.
    """
    available = set(source_columns)
    unknown_inputs = sorted(set(request.group_by) - available)
    unknown_inputs.extend(
        aggregate.column
        for aggregate in request.aggregates
        if aggregate.column is not None and aggregate.column not in available
    )
    if unknown_inputs:
        raise _unknown_output_error(sorted(set(unknown_inputs)), source_columns, catalog, definitions)
    available.update(aggregate.alias for aggregate in request.aggregates)
    output_references = [
        *request.output_columns,
        *(order.column for order in request.order),
        *(condition.column for condition in request.having),
    ]
    unknown_outputs = sorted(set(output_references) - available)
    if unknown_outputs:
        raise _unknown_output_error(unknown_outputs, source_columns, catalog, definitions)


def _unknown_output_error(
    unknown: list[str],
    source_columns: list[str],
    catalog: Catalog | None,
    definitions: dict[str, RelationDefinition] | None = None,
) -> QueryValidationError:
    detail = ""
    if catalog is not None:
        candidates = _nearest_output_columns(unknown, definitions or {}, catalog)
        if candidates:
            detail = f" Did you mean: {', '.join(candidates)}?"
    return QueryValidationError(f"Unknown output columns: {', '.join(unknown)}.{detail}")


def _nearest_output_columns(
    unknown: list[str], definitions: dict[str, RelationDefinition], catalog: Catalog
) -> list[str]:
    """Signed columns close to a wrong guess, from the source it was named against.

    Resolved through the fetched definitions rather than the manifest, so the hint cannot offer a
    relation the request did not fetch, and the candidates come from the signed catalog and nowhere
    else.
    """
    candidates: list[str] = []
    for reference in unknown:
        alias, _, column = reference.rpartition(".")
        definition = definitions.get(alias)
        if definition is None:
            continue
        near = catalog.column_candidates(definition, column)
        if near:
            candidates.append(f"{alias}.{near[0]}")
    return sorted(dict.fromkeys(candidates))


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
    # Dispatched, not looked up in a dict of all eight: a dict literal is evaluated eagerly, so
    # `having mx=eq.3` on a text aggregate also computed `value > expected` and raised a TypeError
    # the tool wrapper does not catch. Only the requested comparison may be computed.
    if condition.operator == FilterOperator.EQ:
        return value == expected
    if condition.operator == FilterOperator.NEQ:
        return value != expected
    if condition.operator == FilterOperator.GT:
        return value > expected
    if condition.operator == FilterOperator.GTE:
        return value >= expected
    if condition.operator == FilterOperator.LT:
        return value < expected
    if condition.operator == FilterOperator.LTE:
        return value <= expected
    if condition.operator == FilterOperator.LIKE:
        return str(value) == str(expected)
    if condition.operator == FilterOperator.ILIKE:
        return str(value).lower() == str(expected).lower()
    raise QueryValidationError(f"Unsupported having operator {condition.operator.value}")


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


def _sort_value(value: Any) -> tuple[int, float | str]:
    """A total order over a value of any type, so `order=` cannot raise.

    A column the manifest declares `string` is not guaranteed to hold strings: PostgREST returns
    whatever JSON type is stored, so a numeric-looking value comes back as a number. Ranking every
    non-null value 1 meant Python compared `(1, 3)` with `(1, '7')` and raised a TypeError that the
    tool wrapper does not catch.

    The type class is the first element, so the second is only ever compared within a class: nulls
    first, then numbers by magnitude, then everything else as lowercase text. Numbers before text is
    an arbitrary choice, and a documented one, which is what makes the order stable.
    """
    if value is None:
        return (0, 0.0)
    if isinstance(value, bool):
        return (1, float(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    return (2, str(value).lower())


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
