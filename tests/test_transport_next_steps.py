"""A transport error that names a failure and no exit leaves the caller guessing.

`test_refusal_next_steps.py` covers the query-validation refusals. This covers the transport layer:
the errors `postgrest.py` and `rpc.py` raise when the upstream is slow, down, or returns something
that is not a list of rows. Each is a dead end today: the caller is told it failed, but not whether
the failure is its own (narrow the request) or the upstream's (retry, then report the endpoint).

The split matters for the same reason as the decline fix: a message that offers no exit invites the
caller to keep rephrasing the query, when the fault is not in the query at all.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest

from benthic_mcp.bdp import CatalogSnapshot
from benthic_mcp.catalog import Catalog
from benthic_mcp.errors import UpstreamError
from benthic_mcp.models import FindDistrictRequest, RelationSource
from benthic_mcp.postgrest import PostgrestTransport
from benthic_mcp.rpc import RpcService


def _relation_source() -> RelationSource:
    return RelationSource(alias="awards", dataset="usaspending", relation="all_entities")


def _rpc_catalog(manifests: dict[str, Any], collection: dict[str, Any]) -> Catalog:
    return Catalog(CatalogSnapshot(collections={"ngopen": collection}, manifests=manifests))


@pytest.mark.asyncio
async def test_a_response_over_the_limit_offers_to_narrow(settings: Any, catalog: Catalog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"uei": "A"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="Narrow the select") as caught:
            await PostgrestTransport(replace(settings, max_response_bytes=10), client).fetch(
                _relation_source(), catalog.resolve_relation("usaspending", "all_entities")
            )
    assert "Narrow the select" in str(caught.value)


@pytest.mark.asyncio
async def test_a_transport_failure_names_the_endpoint_not_the_query(settings: Any, catalog: Catalog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="endpoint is unavailable") as caught:
            await PostgrestTransport(settings, client).fetch(
                _relation_source(), catalog.resolve_relation("usaspending", "all_entities")
            )
    assert "endpoint is unavailable" in str(caught.value)


@pytest.mark.asyncio
async def test_invalid_json_is_blamed_on_the_upstream(settings: Any, catalog: Catalog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="malformed data") as caught:
            await PostgrestTransport(settings, client).fetch(
                _relation_source(), catalog.resolve_relation("usaspending", "all_entities")
            )
    assert "malformed data" in str(caught.value)


@pytest.mark.asyncio
async def test_a_wrong_shaped_result_is_blamed_on_the_upstream(settings: Any, catalog: Catalog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="response shape") as caught:
            await PostgrestTransport(settings, client).fetch(
                _relation_source(), catalog.resolve_relation("usaspending", "all_entities")
            )
    assert "response shape" in str(caught.value)


@pytest.mark.asyncio
async def test_an_rpc_response_over_the_limit_offers_to_narrow(
    settings: Any, manifests: dict[str, Any], collection: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"district_id": "ME-1", "district": "1", "statename": "ME"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="Narrow the request parameters") as caught:
            await RpcService(replace(settings, max_response_bytes=10), client).execute(
                _rpc_catalog(manifests, collection),
                FindDistrictRequest(operation="find_district", lat=43.1, lon=-70.2, congress=118),
            )
    assert "Narrow the request parameters" in str(caught.value)


@pytest.mark.asyncio
async def test_an_rpc_transport_failure_names_the_endpoint(
    settings: Any, manifests: dict[str, Any], collection: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="endpoint is unavailable") as caught:
            await RpcService(settings, client).execute(
                _rpc_catalog(manifests, collection),
                FindDistrictRequest(operation="find_district", lat=43.1, lon=-70.2, congress=118),
            )
    assert "endpoint is unavailable" in str(caught.value)


@pytest.mark.asyncio
async def test_an_rpc_invalid_json_is_blamed_on_the_upstream(
    settings: Any, manifests: dict[str, Any], collection: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="malformed data") as caught:
            await RpcService(settings, client).execute(
                _rpc_catalog(manifests, collection),
                FindDistrictRequest(operation="find_district", lat=43.1, lon=-70.2, congress=118),
            )
    assert "malformed data" in str(caught.value)


@pytest.mark.asyncio
async def test_an_rpc_wrong_shaped_result_is_blamed_on_the_upstream(
    settings: Any, manifests: dict[str, Any], collection: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="response shape") as caught:
            await RpcService(settings, client).execute(
                _rpc_catalog(manifests, collection),
                FindDistrictRequest(operation="find_district", lat=43.1, lon=-70.2, congress=118),
            )
    assert "response shape" in str(caught.value)
