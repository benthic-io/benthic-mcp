from copy import deepcopy
from typing import Any

import pytest

from benthic_mcp.bdp import BdpRepository
from benthic_mcp.errors import CatalogError
from tests.factories import make_collection, sign_document


@pytest.mark.asyncio
async def test_loads_verified_catalog(settings: Any, bdp_client_factory: Any) -> None:
    async with bdp_client_factory() as client:
        repository = BdpRepository(settings, client)
        snapshot = await repository.load()

    assert set(snapshot.collections) == {"ngopen"}
    assert set(snapshot.manifests) == {"usaspending", "samer", "irs_ng", "up_cdmaps", "usp_cl"}


@pytest.mark.asyncio
async def test_rejects_tampered_collection(settings: Any, bdp_documents: dict[str, Any]) -> None:
    import httpx

    tampered = deepcopy(bdp_documents["https://benthic.io/bdp/ngopen/collection.json"])
    tampered["title"] = "tampered"
    documents = {**bdp_documents, "https://benthic.io/bdp/ngopen/collection.json": tampered}

    def handler(request: httpx.Request) -> httpx.Response:
        document = documents.get(str(request.url))
        if document is None:
            return httpx.Response(404)
        return httpx.Response(200, json=document)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CatalogError, match="payload hash"):
            await BdpRepository(settings, client).load()


@pytest.mark.asyncio
async def test_rejects_manifest_url_outside_bdp_root(
    settings: Any,
    manifests: dict[str, Any],
    private_key: Any,
) -> None:
    import httpx

    unsigned = make_collection(private_key, manifests)
    payload = {key: value for key, value in unsigned.items() if key != "cryptographic_signature"}
    payload["members"] = [
        {**member, "manifest_url": "https://example.com/manifest.json"} if index == 0 else member
        for index, member in enumerate(payload["members"])
    ]
    collection = sign_document(payload, private_key)
    documents = {
        "https://benthic.io/bdp/v1/collection.schema.json": {"type": "object"},
        "https://benthic.io/bdp/v1/manifest.schema.json": {"type": "object"},
        "https://benthic.io/bdp/ngopen/collection.json": collection,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        document = documents.get(str(request.url))
        if document is None:
            return httpx.Response(404)
        return httpx.Response(200, json=document)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CatalogError, match="configured origin"):
            await BdpRepository(settings, client).load()
