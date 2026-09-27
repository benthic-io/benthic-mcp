from dataclasses import replace
from typing import Any

import httpx
import pytest

from benthic_mcp.catalog import Catalog
from benthic_mcp.errors import QueryValidationError, UpstreamError
from benthic_mcp.models import FilterOperator, FilterSpec, RelationSource, SourceOrder
from benthic_mcp.postgrest import PostgrestTransport


@pytest.mark.asyncio
async def test_fetch_builds_bounded_postgrest_request(settings: Any, catalog: Catalog) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"uei": "A", "name": "One"}, {"uei": "B", "name": "Two"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = PostgrestTransport(settings, client)
        result = await transport.fetch(
            RelationSource(
                alias="awards",
                dataset="usaspending",
                relation="all_entities",
                select=["uei", "name"],
                filters=[FilterSpec(column="uei", operator=FilterOperator.IN, value=["A", "B"])],
                order=[SourceOrder(column="name")],
                limit=2,
            ),
            catalog.resolve_relation("usaspending", "all_entities"),
        )

    request = requests[0]
    assert request.url.path == "/ngopen/usaspending/all_entities"
    assert request.url.params["select"] == "uei,name"
    assert request.url.params["uei"] == "in.(A,B)"
    assert request.url.params["order"] == "name"
    assert request.url.params["limit"] == "3"
    assert result.rows == [{"uei": "A", "name": "One"}, {"uei": "B", "name": "Two"}]
    assert not result.truncated


@pytest.mark.asyncio
async def test_fetch_truncates_sentinel_row(settings: Any, catalog: Catalog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(4)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(settings, client).fetch(
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities", limit=3),
            catalog.resolve_relation("usaspending", "all_entities"),
        )

    assert len(result.rows) == 3
    assert result.truncated


@pytest.mark.asyncio
async def test_fetch_builds_repeated_filters_as_conjunction(settings: Any, catalog: Catalog) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await PostgrestTransport(settings, client).fetch(
            RelationSource(
                alias="awards",
                dataset="usaspending",
                relation="all_entities",
                filters=[
                    FilterSpec(column="name", operator=FilterOperator.GTE, value="A"),
                    FilterSpec(column="name", operator=FilterOperator.LTE, value="Z"),
                ],
            ),
            catalog.resolve_relation("usaspending", "all_entities"),
        )

    assert requests[0].url.params["and"] == "(name.gte.A,name.lte.Z)"


@pytest.mark.asyncio
async def test_fetch_complete_pages_until_complete(settings: Any, catalog: Catalog) -> None:
    requests: list[httpx.Request] = []
    source_rows = [{"uei": str(index)} for index in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=source_rows[offset : offset + limit])

    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(small_settings, client).fetch_complete(
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            catalog.resolve_relation("usaspending", "all_entities"),
        )

    assert result.rows == source_rows
    assert not result.truncated
    assert [int(request.url.params.get("offset", 0)) for request in requests] == [0, 2, 4]


@pytest.mark.asyncio
async def test_fetch_complete_rejects_scan_over_cap(settings: Any, catalog: Catalog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(offset, offset + limit)])

    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(small_settings, client).fetch_complete(
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            catalog.resolve_relation("usaspending", "all_entities"),
        )

    assert len(result.rows) == 3
    assert result.truncated


@pytest.mark.asyncio
async def test_fetch_rejects_unknown_column(settings: Any, catalog: Catalog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QueryValidationError, match="Unknown columns"):
            await PostgrestTransport(settings, client).fetch(
                RelationSource(
                    alias="awards",
                    dataset="usaspending",
                    relation="all_entities",
                    select=["missing"],
                ),
                catalog.resolve_relation("usaspending", "all_entities"),
            )


@pytest.mark.asyncio
async def test_fetch_enforces_response_size(settings: Any, catalog: Catalog) -> None:
    small_settings = settings.__class__(
        bdp_root=settings.bdp_root,
        collections=settings.collections,
        trusted_keys=settings.trusted_keys,
        cache_dir=settings.cache_dir,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        max_cache_age_seconds=settings.max_cache_age_seconds,
        request_timeout_seconds=settings.request_timeout_seconds,
        max_rows=settings.max_rows,
        default_query_limit=settings.default_query_limit,
        max_response_bytes=10,
        user_agent=settings.user_agent,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"uei": "A"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="exceeded"):
            await PostgrestTransport(small_settings, client).fetch(
                RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
                catalog.resolve_relation("usaspending", "all_entities"),
            )
