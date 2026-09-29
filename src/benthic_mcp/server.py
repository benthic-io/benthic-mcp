import asyncio
import functools
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field, TypeAdapter
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from benthic_mcp.catalog import JoinEdge
from benthic_mcp.config import Settings
from benthic_mcp.errors import BenthicMCPError
from benthic_mcp.models import (
    DiscoverResult,
    PlaybookResult,
    QueryResult,
    ReportResult,
    RpcRequest,
    RpcResult,
)
from benthic_mcp.playbook import BASE_CORE
from benthic_mcp.query import build_single_join, build_single_query, unqualify_result
from benthic_mcp.rpc import RpcOperation, rpc_argument_reference
from benthic_mcp.service import BenthicService
from benthic_mcp.trace import TraceEntry

_operations = rpc_argument_reference()

_service: BenthicService | None = None
_service_lock = asyncio.Lock()

logger = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# benthic_report writes only to the local lesson store, never to BDP, so it is not read-only.
LOCAL_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)

RPC_REQUEST_ADAPTER = TypeAdapter(RpcRequest)

BASE_INSTRUCTIONS = "Use these tools as a read-only analyst over signed Benthic data."

PLAYBOOK_POINTER = (
    "Call benthic_playbook(dataset) first when a question depends on dataset conventions, "
    "then call benthic_report once if this tool call taught you something the playbook lacks."
)

_applied: dict[str, str] = {}


def _apply_core(core: str, report_pointer: str) -> None:
    """Rewrite the always-on guidance in the tool descriptions the calling LLM actually reads."""
    # benthic_discover is always processed so an empty core clears any previously injected block.
    appends = {"benthic_discover": f"Core access rules:\n{core}" if core else ""}
    for name in ("benthic_query", "benthic_join", "benthic_rpc"):
        appends[name] = report_pointer
    for name, extra in appends.items():
        tool = mcp._tool_manager.get_tool(name)
        if tool is None:
            continue
        base = tool.description
        previous = _applied.get(name)
        if previous and base.endswith(previous):
            base = base[: -len(previous)].rstrip()
        if extra:
            tool.description = f"{base}\n\n{extra}"
            _applied[name] = extra
        else:
            tool.description = base
            _applied.pop(name, None)


async def get_service() -> BenthicService:
    global _service
    if _service is not None:
        return _service
    async with _service_lock:
        if _service is None:
            _service = BenthicService(Settings.from_env())
    return _service


async def reset_service() -> None:
    global _service
    async with _service_lock:
        if _service is not None:
            await _service.close()
            _service = None


@asynccontextmanager
async def service_lifespan(server: MCPServer) -> AsyncIterator[None]:
    service = await get_service()
    try:
        runtime = await service.playbook()
    except (BenthicMCPError, httpx.HTTPError) as exc:
        # A catalog outage must not take the server down; serve the static core instead.
        logger.warning("Playbook unavailable, serving the static core: %s", exc)
        _apply_core("\n".join(f"- {rule}" for rule in BASE_CORE), PLAYBOOK_POINTER)
        server._lowlevel_server.instructions = f"{BASE_INSTRUCTIONS}\n\n{PLAYBOOK_POINTER}"
    else:
        # The llama.cpp Web UI reads tool descriptions but not the server `instructions` field,
        # so the always-on guidance is written into both channels here.
        _apply_core(runtime.core, PLAYBOOK_POINTER)
        # MCPServer.instructions has no setter in mcp 2.2.0; the low-level attribute is the
        # only writable handle, and instructions are static for the life of the process.
        server._lowlevel_server.instructions = runtime.instructions()
    try:
        yield
    finally:
        await reset_service()


_swept_at = 0.0


async def _maybe_sweep(service: BenthicService) -> None:
    """Prune expired traces and lessons at most once an hour, off the request path."""
    global _swept_at
    now = time.monotonic()
    if now - _swept_at < 3600:
        return
    _swept_at = now
    await asyncio.to_thread(service.trace_store.sweep)
    await asyncio.to_thread(service.lesson_store.sweep, service.settings.lesson_retention_days)


def _source_hint(arguments: dict[str, Any]) -> list[str]:
    """Schema identifiers only, never user values, so a failed call is still attributable."""
    names = ("source", "left_source", "right_source", "operation", "relation", "dataset")
    return [str(arguments[name]) for name in names if arguments.get(name)]


def _traced[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Record an objective trace for a tool call. Traces never contain argument values."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        service = _service
        started = time.perf_counter()
        entry = TraceEntry(tool=fn.__name__, ok=True, arg_keys=sorted(kwargs), sources=_source_hint(kwargs))
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            entry.ok = False
            entry.error_class = type(exc).__name__
            entry.error = str(exc)[:200]
            raise
        else:
            entry.row_count = getattr(result, "row_count", None)
            entry.truncated = getattr(result, "truncated", None)
            entry.source_complete = getattr(result, "source_complete", None)
            entry.warning_count = len(getattr(result, "warnings", []) or [])
            metadata = getattr(result, "metadata", None)
            if metadata is not None and getattr(metadata, "operation", None):
                entry.sources = [f"rpc:{metadata.operation}"]
            else:
                entry.sources = [source.source for source in getattr(result, "sources", []) or []]
            return result
        finally:
            entry.latency_ms = int((time.perf_counter() - started) * 1000)
            if service is not None:
                service.trace_store.record(entry)

    return wrapper


def _inline_enum_refs() -> None:
    """Flatten $ref in the tool schemas.

    The benthic_rpc enum is emitted as {"$ref": "#/$defs/RpcOperation"}. Clients that forward
    inputSchema verbatim, including the llama.cpp Web UI, are not guaranteed to resolve $ref,
    so the referenced value is copied inline and the $defs block is removed.
    """
    for tool in mcp._tool_manager.list_tools():
        schema = tool.parameters
        definitions = schema.get("$defs")
        if not isinstance(definitions, dict):
            continue
        for spec in schema.get("properties", {}).values():
            reference = spec.get("$ref") if isinstance(spec, dict) else None
            if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                continue
            target = definitions.get(reference.rsplit("/", 1)[-1])
            if isinstance(target, dict):
                spec.pop("$ref")
                spec.update(target)
        schema.pop("$defs", None)


mcp = MCPServer(
    "benthic",
    version="0.1.0",
    instructions=BASE_INSTRUCTIONS,
    lifespan=service_lifespan,
)


@mcp.tool(name="benthic_discover", annotations=READ_ONLY)
@_traced
async def discover(
    query: str = "",
    dataset: str | None = None,
    relation: str | None = None,
    limit: Annotated[int, Field(ge=1, le=8)] = 6,
    detail: Literal["summary", "full"] = "summary",
) -> DiscoverResult:
    """Find the smallest relevant signed source, columns, and join paths for a question.

    Use the returned source field verbatim in benthic_query. If join_paths is empty, make separate
    benthic_query calls; never invent a join.

    One call is normally enough: it returns the best relations together with their columns, and
    columns_truncated says when that list is partial. Ask again for more columns only when
    columns_truncated is true, by passing detail='full'. A wrong column guess is recoverable rather
    than fatal, because benthic_query reports the near-miss signed name.
    """
    try:
        service = await get_service()
        return await service.discover(query=query, dataset=dataset, relation=relation, limit=limit, detail=detail)
    except (BenthicMCPError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(name="benthic_playbook", annotations=READ_ONLY)
async def playbook(
    dataset: str | None = None,
    from_relation: str | None = None,
    to_relation: str | None = None,
    max_hops: Annotated[int, Field(ge=1, le=3)] = 2,
) -> PlaybookResult:
    """Dataset-specific access conventions built from signed metadata and prior sessions.

    Call once per dataset before answering a question that depends on domain conventions: which
    source to use, which columns are authoritative, which mistakes to avoid, and which signed join
    paths and RPC operations exist. Join recipes come from the signed catalog and are never
    invented here. Omit dataset for a routing index of every dataset.

    To move data between two relations, pass from_relation and to_relation as 'dataset.relation'
    instead of a dataset. That returns the signed route hop by hop, shortest first, with the exact
    arguments for each benthic_join call, and nothing else. Do this rather than searching
    benthic_discover for a join path: a search can be rephrased, so a request for a path that does
    not exist comes back looking like a request that was worded wrongly.
    """
    try:
        service = await get_service()
        runtime = await service.playbook()
        if (from_relation is None) != (to_relation is None):
            raise ToolError("Pass both from_relation and to_relation, or neither")
        if from_relation is not None and to_relation is not None:
            return runtime.path_result(from_relation, to_relation, max_hops)
        return runtime.result(dataset)
    except KeyError as exc:
        raise ToolError(f"Unknown dataset {exc.args[0]}; call benthic_playbook() for the list") from exc
    except (BenthicMCPError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(name="benthic_query", annotations=READ_ONLY)
@_traced
async def query(
    question: str,
    source: str,
    select: list[str] | None = None,
    where: list[str] | None = None,
    group_by: list[str] | None = None,
    metrics: list[str] | None = None,
    having: list[str] | None = None,
    order: list[str] | None = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> QueryResult:
    """Run one bounded read-only relation query.

    Call benthic_discover first and pass its source value verbatim. where uses column=operator.value,
    metrics uses alias=function:column, having uses alias>value, and order uses column:asc or column:desc.
    Operators: eq, neq, gt, gte, lt, lte, like, ilike, in, is.null, not.is.null. Aggregates are complete
    or rejected; never infer totals from a truncated source.
    """
    try:
        request = build_single_query(
            question=question,
            source=source,
            select=select,
            where=where,
            group_by=group_by,
            metrics=metrics,
            having=having,
            order=order,
            limit=limit,
            offset=offset,
        )
        service = await get_service()
        return unqualify_result(await service.query(request))
    except (BenthicMCPError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def _unresolved_join_message(left: str, right: str, candidates: list[JoinEdge]) -> str:
    """Why no path was chosen, and what the signed options are.

    Silence here is what produced the retry loop: a caller that asked for a path which does not
    exist, or asked ambiguously, got nothing actionable and asked again.
    """
    if not candidates:
        return (
            f"No signed BDP join connects {left} to {right}. Call "
            f"benthic_playbook(from_relation='{left}', to_relation='{right}') to see what is signed, "
            "or benthic_discover to find a relation that is."
        )
    listed = "; ".join(
        f"{edge.left_column} = {edge.right_column} [{edge.join_type}/{edge.reliability}]" for edge in candidates
    )
    return (
        f"Refusing to choose between signed paths from {left} to {right}: {listed}. "
        "Pass left_column and right_column explicitly, and pass context_conditions for a partial join."
    )


@mcp.tool(name="benthic_join", annotations=READ_ONLY)
@_traced
async def join(
    question: str,
    left_source: str,
    right_source: str,
    left_column: str | None = None,
    right_column: str | None = None,
    left_where: list[str] | None = None,
    right_where: list[str] | None = None,
    context_conditions: list[str] | None = None,
    mode: str = "inner",
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> QueryResult:
    """Run one signed join using qualified sources.

    Omit left_column and right_column when exactly one reliable identifier path connects the two
    sources, and the signed path is resolved for you. That is the common case and it saves having to
    look the path up first. Columns are still required whenever the path is ambiguous, absent, or
    anything other than a reliable identifier join, so a heuristic or partial join is never chosen
    for you; the error lists the candidates instead.

    The adapter follows signed reliability metadata and returns warnings. Partial joins require
    context_conditions. The left key filter is propagated to the right source; do not invent columns
    or unsigned relationships.
    """
    try:
        if (left_column is None) != (right_column is None):
            raise ToolError("Pass both left_column and right_column, or neither")
        if left_column is None and right_column is None:
            service = await get_service()
            edge, candidates = (await service.playbook()).catalog.resolve_join(left_source, right_source)
            if edge is None:
                raise ToolError(_unresolved_join_message(left_source, right_source, candidates))
            left_column, right_column = edge.left_column, edge.right_column
        assert left_column is not None and right_column is not None
        request = build_single_join(
            question=question,
            left_source=left_source,
            right_source=right_source,
            left_column=left_column,
            right_column=right_column,
            left_where=left_where,
            right_where=right_where,
            context_conditions=context_conditions,
            mode=mode,
            limit=limit,
        )
        service = await get_service()
        return await service.query(request)
    except (BenthicMCPError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(name="benthic_rpc", annotations=READ_ONLY)
@_traced
async def rpc(
    operation: RpcOperation,
    lat: float | None = None,
    lon: float | None = None,
    congress: int | None = None,
    min_lat: float | None = None,
    max_lat: float | None = None,
    min_lon: float | None = None,
    max_lon: float | None = None,
    radius_meters: int | None = None,
) -> RpcResult:
    f"""Run an allowlisted read-only spatial operation exposed by this server.

    Operations and their arguments: {_operations}. Use the returned row count, truncation flag, and
    scope warning. Do not infer geographic identity beyond the returned evidence.
    """
    try:
        arguments = {
            "lat": lat,
            "lon": lon,
            "congress": congress,
            "min_lat": min_lat,
            "max_lat": max_lat,
            "min_lon": min_lon,
            "max_lon": max_lon,
            "radius_meters": radius_meters,
        }
        values: dict[str, Any] = {"operation": operation.value}
        values.update({key: value for key, value in arguments.items() if value is not None})
        request = RPC_REQUEST_ADAPTER.validate_python(values)
        service = await get_service()
        return await service.rpc(request)
    except (BenthicMCPError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(name="benthic_report", annotations=LOCAL_WRITE)
async def report(
    symptom: str,
    lesson: str,
    question_summary: str = "",
    dataset: str | None = None,
    relation: str | None = None,
    confidence: Literal["high", "medium", "low"] = "medium",
) -> ReportResult:
    """Record one mistake you made and the correction, so future sessions do not repeat it.

    Call this once after answering, only if a tool call or a source convention misled you. Write
    the lesson as an instruction to your future self ("use X instead of Y"), not as a complaint.
    Reports are stored as pending and are only served to later sessions after catalog verification
    and an offline A/B evaluation, so a wrong report cannot change current behaviour. Do not call
    it when nothing went wrong.
    """
    try:
        service = await get_service()
        result = await service.record_lesson(
            symptom=symptom,
            lesson=lesson,
            question_summary=question_summary,
            dataset=dataset,
            relation=relation,
            confidence=confidence,
        )
        await _maybe_sweep(service)
        return result
    except (BenthicMCPError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


_inline_enum_refs()


class StaticTokenVerifier:
    def __init__(self, token: str) -> None:
        self._token = token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token.encode("utf-8"), self._token):
            return None
        return AccessToken(token=token, client_id="benthic-ui", scopes=["benthic:read"])


class HttpRequireAuthMiddleware(RequireAuthMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def create_http_app(settings: Settings) -> Starlette:
    if settings.mcp_bearer_token is None:
        raise ValueError("BENTHIC_MCP_BEARER_TOKEN is required for HTTP transport")

    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(settings.mcp_allowed_hosts),
            allowed_origins=list(settings.mcp_allowed_origins),
        ),
        host=settings.mcp_host,
    )
    app.add_middleware(HttpRequireAuthMiddleware, required_scopes=["benthic:read"])
    app.add_middleware(
        AuthenticationMiddleware,
        backend=BearerAuthBackend(StaticTokenVerifier(settings.mcp_bearer_token)),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.mcp_allowed_origins),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "Mcp-Session-Id",
            "MCP-Protocol-Version",
        ],
        expose_headers=["Mcp-Session-Id"],
        max_age=600,
    )
    return app


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "benthic-mcp", "version": "0.1.0"})


def main() -> None:
    settings = Settings.from_env()
    if settings.mcp_transport == "stdio":
        # No token is required or consulted: stdio has no HTTP layer to authenticate, and the
        # process is a child of the agent that spawned it rather than a service on a port. The
        # signed catalog, the playbook, and the trust boundary are all unchanged.
        mcp.run(transport="stdio")
        return
    app = create_http_app(settings)
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port, log_level="info")
