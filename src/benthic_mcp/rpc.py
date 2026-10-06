import json
from dataclasses import dataclass
from enum import StrEnum

import httpx

from benthic_mcp.catalog import Catalog
from benthic_mcp.config import Settings
from benthic_mcp.errors import QueryValidationError, UpstreamError
from benthic_mcp.models import (
    DistrictBboxRequest,
    FindDistrictRequest,
    NonprofitsNearbyRequest,
    RpcMetadata,
    RpcRequest,
    RpcResult,
)


@dataclass(frozen=True, slots=True)
class RpcDefinition:
    dataset: str
    function_name: str
    summary: str
    required_arguments: tuple[str, ...]
    optional_arguments: tuple[str, ...] = ()


class RpcOperation(StrEnum):
    """The allowlist. This is the single source of truth for the benthic_rpc operation enum."""

    FIND_DISTRICT = "find_district"
    DISTRICTS_IN_BBOX = "districts_in_bbox"
    NONPROFITS_NEARBY = "nonprofits_nearby"


RPC_DEFINITIONS: dict[RpcOperation, RpcDefinition] = {
    RpcOperation.FIND_DISTRICT: RpcDefinition(
        dataset="up_cdmaps",
        function_name="rpc_find_district",
        summary="Congressional district containing a point.",
        required_arguments=("lat", "lon"),
        optional_arguments=("congress",),
    ),
    RpcOperation.DISTRICTS_IN_BBOX: RpcDefinition(
        dataset="up_cdmaps",
        function_name="rpc_districts_in_bbox",
        summary="Districts intersecting a bounding box.",
        required_arguments=("min_lat", "max_lat", "min_lon", "max_lon"),
        optional_arguments=("congress",),
    ),
    RpcOperation.NONPROFITS_NEARBY: RpcDefinition(
        dataset="irs_ng",
        function_name="rpc_nonprofits_nearby",
        summary="Nonprofit organizations near a point.",
        required_arguments=("lat", "lon"),
        optional_arguments=("radius_meters",),
    ),
}


def rpc_argument_reference() -> str:
    """Compact operation -> argument reference, injected into the benthic_rpc description."""

    def describe(operation: RpcOperation) -> str:
        definition = RPC_DEFINITIONS[operation]
        arguments = [
            *definition.required_arguments,
            *(f"{argument}?" for argument in definition.optional_arguments),
        ]
        return f"{operation.value}({', '.join(arguments)})"

    return "; ".join(describe(operation) for operation in RPC_DEFINITIONS)


def rpc_operation_reference() -> str:
    """Compact operation -> what-it-answers reference, injected into the benthic_rpc description.

    The arguments alone do not tell a caller which question an operation answers. Without this the
    model has to call each one to find out, and each call is a turn.
    """
    return "\n".join(f"{operation.value}: {definition.summary}" for operation, definition in RPC_DEFINITIONS.items())


class RpcService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def execute(self, catalog: Catalog, request: RpcRequest) -> RpcResult:
        definition = RPC_DEFINITIONS[RpcOperation(request.operation)]
        endpoint = catalog.endpoint_for(definition.dataset)
        url = f"{endpoint}rpc/{definition.function_name}"
        self._validate_endpoint(url)
        params = request.model_dump(mode="json", exclude={"operation"}, exclude_none=True)

        try:
            async with self.client.stream("GET", url, params=params) as response:
                if response.status_code >= 400:
                    body = (await response.aread())[:4096].decode("utf-8", errors="replace")
                    raise UpstreamError(f"Benthic RPC {request.operation} returned {response.status_code}: {body}")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.settings.max_response_bytes:
                        raise UpstreamError(
                            f"Benthic RPC {request.operation} exceeded {self.settings.max_response_bytes} bytes"
                        )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Benthic RPC {request.operation} failed: {exc}") from exc

        try:
            rows = json.loads(content)
        except json.JSONDecodeError as exc:
            raise UpstreamError(f"Benthic RPC {request.operation} returned invalid JSON") from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise UpstreamError(f"Benthic RPC {request.operation} returned an unexpected result")

        return RpcResult(
            rows=rows[: self.settings.max_rows],
            row_count=min(len(rows), self.settings.max_rows),
            truncated=len(rows) > self.settings.max_rows,
            metadata=RpcMetadata(
                operation=request.operation,
                dataset=definition.dataset,
                function_name=definition.function_name,
                endpoint=endpoint,
                request_url=str(httpx.URL(url, params=params)),
            ),
            warnings=["Spatial results depend on the requested point, bounding box, radius, and Congress parameter."],
        )

    @staticmethod
    def _validate_endpoint(url: str) -> None:
        parsed = httpx.URL(url)
        host = parsed.host.lower()
        if parsed.scheme != "https" or not (host == "benthic.io" or host.endswith(".benthic.io")):
            raise QueryValidationError("Benthic RPC endpoint must use HTTPS on benthic.io")


def validate_rpc_request(request: RpcRequest) -> None:
    if isinstance(request, FindDistrictRequest):
        return
    if isinstance(request, DistrictBboxRequest):
        return
    if isinstance(request, NonprofitsNearbyRequest):
        return
    raise QueryValidationError("Unsupported Benthic RPC operation")
