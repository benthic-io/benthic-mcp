from dataclasses import replace
from typing import Any

import httpx
import pytest

from benthic_mcp.bdp import BdpRepository
from benthic_mcp.errors import QueryValidationError
from benthic_mcp.models import (
    AggregateFunction,
    AggregateSpec,
    FilterOperator,
    FilterSpec,
    HavingSpec,
    JoinMode,
    JoinSpec,
    OutputOrder,
    QueryRequest,
    RelationSource,
    Reliability,
)
from benthic_mcp.postgrest import PostgrestTransport
from benthic_mcp.query import QueryService, build_single_query, unqualify_result


def _query_client(bdp_documents: dict[str, Any], rows_by_path: dict[str, list[dict[str, Any]]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        path = request.url.path
        if path in rows_by_path:
            return httpx.Response(200, json=rows_by_path[path])
        return httpx.Response(404, json={"error": "not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_executes_signed_multi_dataset_query(settings: Any, bdp_documents: dict[str, Any]) -> None:
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [
                {"uei": "A", "name": "Recipient A", "state": "ME"},
                {"uei": "B", "name": "Recipient B", "state": "ME"},
            ],
            "/ngopen/samer/sam_registrations": [
                {"uei": "A", "name": "SAM A"},
                {"uei": "B", "name": "SAM B"},
            ],
        },
    )
    service = QueryService(
        settings,
        BdpRepository(settings, client),
        PostgrestTransport(settings, client),
    )
    request = QueryRequest(
        question="Show recipient registrations",
        sources=[
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            RelationSource(alias="sam", dataset="samer", relation="sam_registrations"),
        ],
        joins=[JoinSpec(left_alias="awards", right_alias="sam", left_column="uei", right_column="uei")],
        allowed_reliability=[Reliability.RELIABLE],
    )

    result = await service.execute(request)
    await client.aclose()

    assert result.row_count == 2
    assert result.rows[0]["awards.name"] == "Recipient A"
    assert result.rows[0]["sam.name"] == "SAM A"
    assert result.joins[0].reliability == Reliability.RELIABLE


@pytest.mark.asyncio
async def test_aggregates_and_orders_joined_rows(settings: Any, bdp_documents: dict[str, Any]) -> None:
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [
                {"uei": "A", "state": "ME"},
                {"uei": "B", "state": "ME"},
                {"uei": "C", "state": "VT"},
            ],
            "/ngopen/samer/sam_registrations": [
                {"uei": "A"},
                {"uei": "B"},
            ],
        },
    )
    service = QueryService(
        settings,
        BdpRepository(settings, client),
        PostgrestTransport(settings, client),
    )
    request = QueryRequest(
        question="Count registered recipients by state",
        sources=[
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            RelationSource(alias="sam", dataset="samer", relation="sam_registrations"),
        ],
        joins=[
            JoinSpec(
                left_alias="awards",
                right_alias="sam",
                left_column="uei",
                right_column="uei",
                mode=JoinMode.LEFT,
            )
        ],
        group_by=["awards.state"],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, alias="recipient_count")],
        order=[OutputOrder(column="awards.state")],
    )

    result = await service.execute(request)
    await client.aclose()

    assert result.rows == [
        {"awards.state": "ME", "recipient_count": 2},
        {"awards.state": "VT", "recipient_count": 1},
    ]


@pytest.mark.asyncio
async def test_single_query_builds_filters_having_and_unqualified_output(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    source_rows = [
        {"uei": "A", "name": "A", "state": "ME", "total_obligation": 60000},
        {"uei": "A", "name": "A", "state": "ME", "total_obligation": 50000},
        {"uei": "B", "name": "B", "state": "VT", "total_obligation": 70000},
        {"uei": "B", "name": "B", "state": "VT", "total_obligation": 200},
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-3/4"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=source_rows[offset : offset + limit])

    request = build_single_query(
        question="Organizations over 100000",
        dataset="usaspending",
        relation="all_entities",
        select=None,
        where=["state=eq.ME"],
        group_by=["uei", "name", "state"],
        metrics=["total=sum:total_obligation"],
        having=["total>100000"],
        order=["total:desc"],
        limit=100,
        offset=0,
    )
    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings,
            BdpRepository(small_settings, client),
            PostgrestTransport(small_settings, client),
        )
        result = unqualify_result(await service.execute(request))

    assert result.source_complete
    assert result.rows == [{"uei": "A", "name": "A", "state": "ME", "total": 110000}]
    assert requests[0].url.params["state"] == "eq.ME"
    assert len(requests[0].url.params.get_list("state")) == 1
    assert len(requests) >= 2


@pytest.mark.asyncio
async def test_rejects_incomplete_aggregate_scan(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            # No count, so this is the scan that has to run out of its budget to discover the cap.
            return httpx.Response(200)
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(offset, offset + limit)])

    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=3)
    request = QueryRequest(
        question="Count recipients",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities")],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, alias="recipient_count")],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings,
            BdpRepository(small_settings, client),
            PostgrestTransport(small_settings, client),
        )
        with pytest.raises(QueryValidationError, match="complete-scan limit") as caught:
            await service.execute(request)

    # With no count to go on, the cap is the only size the refusal can name, and it names it anyway.
    assert "More than 3 rows match" in str(caught.value)
    assert "usaspending.all_entities" in str(caught.value)


@pytest.mark.asyncio
async def test_an_over_cap_refusal_names_the_row_count_and_how_far_over_it_is(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """The refusal used to say only "narrow the filters", to a caller that had already narrowed.

    `congressional_district=eq.03` is a filter that looks narrow and matches 1416153 of the rows in
    usaspending.all_entities, 141 times the limit. Told only to narrow, a caller cannot tell that
    no amount of narrowing gets it under 10000, so it retries filters and gets told to narrow again.
    The count is the whole difference between those two situations, so it has to be in the message.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-1416152/1416153"})
        raise AssertionError(f"the count is the refusal, so no page should be sent, but {request.method} was")

    small_settings = replace(settings, max_rows=1000, aggregate_scan_limit=10_000)
    request = QueryRequest(
        question="Total obligations in district 3",
        sources=[
            RelationSource(
                alias="s",
                dataset="usaspending",
                relation="all_entities",
                filters=[FilterSpec(column="congressional_district", operator=FilterOperator.EQ, value="03")],
            )
        ],
        aggregates=[AggregateSpec(function=AggregateFunction.SUM, column="total_obligation", alias="total")],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings,
            BdpRepository(small_settings, client),
            PostgrestTransport(small_settings, client),
        )
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(request)

    message = str(caught.value)
    assert "1416153 rows match" in message
    assert "1406153 more than the complete-scan limit of 10000" in message
    assert "usaspending.all_entities" in message
    # The count is only worth anything if it counts the rows this query asked for.
    assert requests[0].url.params["congressional_district"] == "eq.03"
    assert [request.method for request in requests] == ["HEAD"]


@pytest.mark.asyncio
async def test_a_join_over_the_row_limit_does_not_blame_the_scan_limit(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """Both sources here are scanned in full and are nowhere near the scan limit. The join is what
    produced too many rows, and the old message sent the caller to raise a limit that cannot fix it.
    """
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [{"uei": "A"}, {"uei": "A"}],
            "/ngopen/samer/sam_registrations": [{"uei": "A"}, {"uei": "A"}, {"uei": "A"}],
        },
    )
    small_settings = replace(settings, max_rows=4, aggregate_scan_limit=10)
    service = QueryService(
        small_settings,
        BdpRepository(small_settings, client),
        PostgrestTransport(small_settings, client),
    )
    try:
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(
                QueryRequest(
                    question="Match every recipient to every registration",
                    sources=[
                        RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
                        RelationSource(alias="sam", dataset="samer", relation="sam_registrations"),
                    ],
                    joins=[JoinSpec(left_alias="awards", right_alias="sam", left_column="uei", right_column="uei")],
                    allowed_reliability=[Reliability.RELIABLE],
                )
            )
    finally:
        await client.aclose()

    message = str(caught.value)
    assert "BENTHIC_MAX_ROWS" in message
    assert "BENTHIC_AGGREGATE_SCAN_LIMIT" not in message


@pytest.mark.asyncio
async def test_rejects_multiple_unjoined_sources(settings: Any, bdp_documents: dict[str, Any]) -> None:
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [],
            "/ngopen/samer/sam_registrations": [],
        },
    )
    service = QueryService(
        settings,
        BdpRepository(settings, client),
        PostgrestTransport(settings, client),
    )

    with pytest.raises(QueryValidationError, match="require at least one signed join"):
        await service.execute(
            QueryRequest(
                question="Compare datasets",
                sources=[
                    RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
                    RelationSource(alias="sam", dataset="samer", relation="sam_registrations"),
                ],
            )
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_zero_rows_preserve_explicit_output_columns(settings: Any, bdp_documents: dict[str, Any]) -> None:
    client = _query_client(bdp_documents, {"/ngopen/usaspending/all_entities": []})
    service = QueryService(
        settings,
        BdpRepository(settings, client),
        PostgrestTransport(settings, client),
    )

    result = await service.execute(
        QueryRequest(
            question="Find one entity",
            sources=[
                RelationSource(
                    alias="awards",
                    dataset="usaspending",
                    relation="all_entities",
                    select=["uei"],
                    filters=[FilterSpec(column="uei", operator=FilterOperator.EQ, value="missing")],
                )
            ],
            output_columns=["awards.uei"],
        )
    )
    await client.aclose()

    assert result.rows == []
    assert result.columns == ["awards.uei"]


@pytest.mark.asyncio
async def test_ordering_a_column_of_mixed_types_is_a_total_order_not_a_crash(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """`order=` raised an uncaught TypeError on a column the manifest calls `string`.

    A declared `string` column is not guaranteed to hold strings: PostgREST returns whatever JSON
    type is stored, and a numeric-looking value comes back as a number. `_sort_value` tagged numbers
    and text with the same rank 1, so comparing `(1, 3)` with `(1, '7')` raised
    `'<' not supported between instances of 'str' and 'int'`. That escaped the tool wrapper, which
    catches BenthicMCPError and ValueError but not TypeError, so the caller got an internal error
    rather than a result. Found by contract-checking the sort key's totality, invisible to 670
    case-runs because no case orders by such a column.
    """
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [
                {"uei": "A", "name": 3},
                {"uei": "B", "name": "alpha"},
                {"uei": "C", "name": 1},
                {"uei": "D", "name": "beta"},
            ]
        },
    )
    service = QueryService(settings, BdpRepository(settings, client), PostgrestTransport(settings, client))
    request = QueryRequest(
        question="Order by name",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "name"])],
        order=[OutputOrder(column="s.name")],
    )

    try:
        result = unqualify_result(await service.execute(request))
    finally:
        await client.aclose()

    # Total order, deterministic, and every row present: sorting must not drop or invent rows.
    # Documented order: nulls, then numbers by magnitude, then text as lowercase.
    assert [row["uei"] for row in result.rows] == ["C", "A", "B", "D"]
    assert result.row_count == 4


@pytest.mark.asyncio
async def test_having_never_compares_text_with_a_number(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """`having` built a dict of all eight comparisons, so an equality also computed `>`.

    The dict literal is eager, so `having mx=eq.3` where `mx` is min() over a text column evaluated
    `value > expected` and raised `'>' not supported between instances of 'str' and 'int'`. The
    ordering operators are guarded by _numeric and refuse cleanly; equality and inequality were not,
    because nothing stopped the dict from computing the unguarded ones as well. TypeError is not a
    BenthicMCPError and not a ValueError, so it escaped the tool wrapper and the caller saw an
    internal error instead of a message naming the column. Same family as the sort-key crash, one
    layer over.
    """
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [
                {"uei": "A", "name": "alpha"},
                {"uei": "B", "name": "beta"},
            ]
        },
    )
    service = QueryService(settings, BdpRepository(settings, client), PostgrestTransport(settings, client))
    request = QueryRequest(
        question="Having an equality on a text aggregate",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "name"])],
        aggregates=[AggregateSpec(alias="mx", function=AggregateFunction.MIN, column="s.name")],
        having=[HavingSpec(column="mx", operator=FilterOperator.EQ, value=3)],
    )

    ordering = request.model_copy(update={"having": [HavingSpec(column="mx", operator=FilterOperator.GT, value=3)]})
    try:
        equality = unqualify_result(await service.execute(request))
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(ordering)
    finally:
        await client.aclose()

    assert equality.rows == [], "no text equals 3, so the answer is an empty result, not a crash"
    # The ordering operators still refuse, and the refusal names the column so the caller can act.
    assert "mx" in str(caught.value)


def _built(metrics: list[str] | None = None, having: list[str] | None = None) -> QueryRequest:
    return build_single_query(
        question="Total obligations by state",
        dataset="usaspending",
        relation="all_entities",
        select=None,
        where=None,
        group_by=["state"],
        metrics=metrics,
        having=having,
        order=None,
        limit=100,
        offset=0,
    )


def test_both_metric_spellings_build_the_same_aggregate() -> None:
    # The ':' spelling is accepted and was documented nowhere, so a caller who found it by trying
    # had no reason to prefer it and no reason to trust it.
    assert (
        _built(metrics=["total=sum:total_obligation"]).aggregates
        == _built(metrics=["total:sum:total_obligation"]).aggregates
    )


def test_a_bare_function_column_metric_is_rejected() -> None:
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["sum:total_obligation"])

    message = str(caught.value)
    assert "total_sum=sum:total_obligation" in message
    assert "The output name is missing" in message


@pytest.mark.parametrize("expression", ["total:sum:obligation:extra", "total=sum:obligation:extra"])
def test_a_column_containing_a_colon_is_rejected_where_it_is_typed(expression: str) -> None:
    """The capped split accepted a column of 'obligation:extra' and failed much later on it.

    The failure it reached instead was an unknown-column error from the fetched relation, so the
    caller was sent looking for a column that had never been asked for.
    """
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=[expression])

    message = str(caught.value)
    assert "total_sum=sum:total_obligation" in message
    # The reason names the colon: a name was given, so the missing-name reason would send the
    # caller to fix the part that is already right.
    assert "A column cannot contain ':'." in message


def test_an_empty_metric_column_is_a_named_error_and_not_a_pydantic_one() -> None:
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["total_sum=sum:"])

    assert "The source column is empty." in str(caught.value)


def test_whitespace_around_every_part_of_a_metric_is_tolerated() -> None:
    # Only the name was stripped, so 'n = sum : col' was reported as an unknown function ' sum '.
    assert (
        _built(metrics=[" total = sum : total_obligation "]).aggregates
        == _built(metrics=["total=sum:total_obligation"]).aggregates
    )


def test_an_empty_metric_name_is_a_named_error_and_not_a_pydantic_one() -> None:
    """An empty name reached AggregateSpec, so the caller read 'String should match pattern'.

    The tool wrapper catches ValueError, which a pydantic failure is, so the pattern text was
    served to the caller as the whole error: no form, no example, nothing to act on.
    """
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["=sum:total_obligation"])

    message = str(caught.value)
    assert type(caught.value) is QueryValidationError
    assert "total_sum=sum:total_obligation" in message
    assert "String should match" not in message


@pytest.mark.parametrize("expression", ["sum:total_obligation", "=sum:total_obligation", "total_sum"])
def test_the_metric_error_carries_an_example_that_parses(expression: str) -> None:
    """The point of the example is that a caller can copy it, so the test uses it.

    The message used to restate the form with no example, and the form said "name" where the tool
    docstring said "alias", so a caller read it as the source column and its correction, which was
    valid, was never confirmed.
    """
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=[expression])

    assert "total_sum=sum:total_obligation" in str(caught.value)
    assert _built(metrics=["total_sum=sum:total_obligation"]).aggregates[0].alias == "total_sum"


@pytest.mark.parametrize(
    "expression",
    ["total_sum", "total_sum:100000", ":gt:100000", ">100000"],
)
def test_a_malformed_having_names_both_accepted_forms(expression: str) -> None:
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["total_sum=sum:total_obligation"], having=[expression])

    message = str(caught.value)
    assert "total_sum>100000" in message
    assert "total_sum:gt:100000" in message
    assert "String should match" not in message


def test_an_unknown_having_operator_lists_the_accepted_ones() -> None:
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["total_sum=sum:total_obligation"], having=["total_sum:above:100000"])

    message = str(caught.value)
    assert "above" in message
    # ne is not a name this parser has; neq is, and it is the one a caller reading the filter
    # operators would have written otherwise.
    for operator in ("eq", "neq", "gt", "gte", "lt", "lte"):
        assert operator in message


def test_the_having_example_the_message_gives_parses() -> None:
    assert _built(
        metrics=["total_sum=sum:total_obligation"],
        having=["total_sum>100000", "total_sum:gt:100000"],
    ).having == [
        HavingSpec(column="total_sum", operator=FilterOperator.GT, value=100000),
        HavingSpec(column="total_sum", operator=FilterOperator.GT, value=100000),
    ]


@pytest.mark.asyncio
async def test_a_bare_count_is_answered_from_the_count_head_rather_than_refused(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """A `count(*)` with no group_by produces one row whatever the source width, so the scan limit
    does not apply to it - but it was being refused anyway, and a model cannot narrow a count.

    Found by driving the live interface: `count(*)` on usaspending.reporting_agency_overview was
    refused at 10,545 rows against a 10,000 limit, and `count(*)` on usaspending.all_entities
    filtered to district 03 was refused at 1,416,153. There is no filter that makes either
    succeed, so both were dead ends, and the model burned its whole turn budget re-issuing them.

    PostgREST answers it without a scan. `select=count` is not gated by the server's
    db-aggregates-enabled setting (every `count()` and `col.sum()` spelling returns PGRST123), and
    the count HEAD this path already sends carries the exact total in Content-Range: verified
    `[{"count": 1416153}]` in 1.23s against a 17,884,243-row table. The scan is pure waste here.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-1416152/1416153"})
        # Any GET here would be the scan the count makes unnecessary.
        return httpx.Response(200, json=[])

    capped = replace(settings, max_rows=2, aggregate_scan_limit=3)
    request = QueryRequest(
        question="Count recipients in district 03",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities")],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column=None, alias="n")],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    assert result.rows == [{"n": 1416153}], f"expected the exact count, got {result.rows}"
    assert not [r for r in requests if r.method == "GET" and r.url.params.get("limit")], (
        "a bare count must not page the source"
    )


@pytest.mark.asyncio
async def test_a_bare_count_falls_back_to_the_scan_when_the_count_head_answers_nothing(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """The count HEAD is load-bearing for this shape now, so a silent failure must never produce a
    number. `count_matching` returns None on any failure by design; that has to mean "scan instead",
    not "answer zero"."""

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(405)  # count not supported here
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        # Bounded, so the scan terminates under the cap and the assertion is about which path
        # produced the number rather than about the guard.
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(offset, min(offset + limit, 5))])

    capped = replace(settings, max_rows=2, aggregate_scan_limit=10)
    request = QueryRequest(
        question="Count recipients",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities")],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column=None, alias="n")],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    assert result.rows == [{"n": 5}], f"must fall back to the scan and count it, got {result.rows}"


@pytest.mark.asyncio
async def test_a_grouped_count_still_uses_the_scan(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """The fast path is deliberately narrow. A grouped count has one output row per distinct group
    value, so its size depends on the data rather than on the source width, and the count HEAD
    cannot answer it - it would silently return the ungrouped total, which is a different number."""

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            # No count available, so neither the fast path nor the guard can fire and the only way
            # to answer is to scan and group. If the fast path were reachable for a grouped count it
            # would either refuse (a count of "everything" over the cap) or answer one row with the
            # ungrouped total; both fail the assertions below.
            return httpx.Response(200)
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        rows = [
            {"uei": str(index), "state": "MA" if index % 2 else "VT"} for index in range(offset, min(offset + limit, 8))
        ]
        return httpx.Response(200, json=rows)

    capped = replace(settings, max_rows=4, aggregate_scan_limit=10)
    request = QueryRequest(
        question="Count recipients by state",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "state"])],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column=None, alias="n")],
        group_by=["s.state"],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    assert len(result.rows) == 2, f"a grouped count must produce one row per group, got {result.rows}"
    assert {row["s.state"] for row in result.rows} == {"MA", "VT"}
    assert sorted(row["n"] for row in result.rows) == [4, 4], f"expected an even split of 8, got {result.rows}"


@pytest.mark.asyncio
async def test_the_bare_count_fast_path_is_not_reachable_when_the_count_would_overflow_the_guard(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """The fast path must be refused by shape, not merely be unreachable because of the cap.

    With a `group_by`, the count HEAD answers the number of matching *source* rows, which is not the
    number of output rows. Taking that path would answer `count(*)` over the whole filter and label
    it as a grouped result - a confidently wrong number rather than a refusal. The grouped contract
    above only passes today because the scan cap happens to stop it first, so it does not prove the
    shape is excluded. This one makes the cap irrelevant: the count is small enough to scan, and a
    fast path that had accepted the group_by would answer one row instead of one row per group.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-7/8"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(
            200,
            json=[
                {"uei": str(index), "state": "MA" if index % 2 else "VT"}
                for index in range(offset, min(offset + limit, 8))
            ],
        )

    capped = replace(settings, max_rows=10, aggregate_scan_limit=100)
    request = QueryRequest(
        question="Count recipients by state",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "state"])],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column=None, alias="n")],
        group_by=["s.state"],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    # A fast path that ignored the group_by would have returned [{"n": 8}] - one row, the ungrouped
    # total. Grouping 8 rows on a two-valued column has to give two rows.
    assert len(result.rows) == 2, f"the grouped shape was answered without grouping: {result.rows}"
    assert sorted(row["n"] for row in result.rows) == [4, 4]


@pytest.mark.asyncio
async def test_a_count_of_a_column_does_not_take_the_row_count(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """`count(column)` counts non-null values and `count(*)` counts rows. They are different
    numbers whenever the column has nulls, and the count HEAD only knows the row count.

    Letting `count(column)` onto the fast path answers the row count under the column's alias, which
    is a confidently wrong total rather than a refusal - and IRS organization tables are exactly the
    shape where `f990_total_assets_recent` is null for most rows. The scan is required.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-7/8"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        # Four of eight rows have the column; the rest are null.
        rows = [
            {"uei": str(index), "duns": str(index) if index % 2 == 0 else None}
            for index in range(offset, min(offset + limit, 8))
        ]
        return httpx.Response(200, json=rows)

    capped = replace(settings, max_rows=10, aggregate_scan_limit=100)
    request = QueryRequest(
        question="How many entities have a DUNS recorded",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "duns"])],
        output_columns=["n"],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column="s.duns", alias="n")],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    assert result.rows == [{"n": 4}], f"count of a column must count non-null values, not rows; got {result.rows}"
