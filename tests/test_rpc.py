from typing import Any

import httpx
import pytest

from benthic_mcp.bdp import CatalogSnapshot
from benthic_mcp.catalog import Catalog
from benthic_mcp.models import FindDistrictRequest
from benthic_mcp.rpc import RpcService


@pytest.mark.asyncio
async def test_executes_allowlisted_rpc(settings: Any, manifests: dict[str, Any], collection: dict[str, Any]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[{"district_id": "ME-1", "district": "1", "statename": "ME"}],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await RpcService(settings, client).execute(
            Catalog(CatalogSnapshot(collections={"ngopen": collection}, manifests=manifests)),
            FindDistrictRequest(operation="find_district", lat=43.1, lon=-70.2, congress=118),
        )

    assert requests[0].url.path == "/ngopen/up_cdmaps/rpc/rpc_find_district"
    assert requests[0].url.params["lat"] == "43.1"
    assert requests[0].url.params["lon"] == "-70.2"
    assert result.row_count == 1
    assert result.rows[0]["district_id"] == "ME-1"
