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
        with pytest.raises(QueryValidationError, match="complete-scan limit"):
            await service.execute(request)


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
