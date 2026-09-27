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
