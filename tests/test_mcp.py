import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.types import TextContent

from benthic_mcp import server as server_module
from benthic_mcp.catalog import Catalog
from benthic_mcp.config import Settings
from benthic_mcp.models import (
    DiscoverResult,
    FindDistrictRequest,
    QueryRequest,
    QueryResult,
    ReportResult,
    RpcMetadata,
    RpcResult,
)
from benthic_mcp.playbook import VerifyReport
from benthic_mcp.service import BenthicService, PlaybookRuntime
from benthic_mcp.trace import LessonStore, TraceStore


class FakeService:
    """A real BenthicService with the catalog injected, so no network is touched.

    Wrapping the real service rather than faking its methods keeps the report path under test.
    """

    def __init__(self, catalog: Catalog, settings: Settings) -> None:
        self._real = BenthicService(replace(settings, cache_dir=settings.cache_dir))
        self._real._playbook = PlaybookRuntime(
            playbook=None, catalog=catalog, status="disabled", report=VerifyReport(), warnings=[], token_budget=600
        )

    @property
    def settings(self) -> Settings:
        return self._real.settings

    @property
    def trace_store(self) -> TraceStore:
        return self._real.trace_store

    @property
    def lesson_store(self) -> LessonStore:
        return self._real.lesson_store

    async def close(self) -> None:
        await self._real.close()

    async def playbook(self) -> PlaybookRuntime:
        return await self._real.playbook()

    async def record_lesson(self, **kwargs: Any) -> ReportResult:
        return await self._real.record_lesson(**kwargs)

    async def discover(self, **kwargs: Any) -> DiscoverResult:
        return DiscoverResult(
            query=str(kwargs.get("query", "")),
            relations=[],
            join_paths=[],
            total_matches=0,
            more_available=False,
            warnings=[],
        )

    async def query(self, request: QueryRequest) -> QueryResult:
        return QueryResult(
            columns=[],
            rows=[],
            row_count=0,
            source_complete=True,
            truncated=False,
            sources=[],
            joins=[],
        )

    async def rpc(self, request: FindDistrictRequest) -> RpcResult:
        return RpcResult(
            rows=[{"district_id": "ME-1"}],
            row_count=1,
            truncated=False,
            metadata=RpcMetadata(
                operation=request.operation,
                dataset="up_cdmaps",
                function_name="rpc_find_district",
                endpoint="https://benthic.io/ngopen/up_cdmaps/",
                request_url="https://benthic.io/ngopen/up_cdmaps/rpc/rpc_find_district",
            ),
        )


@pytest.mark.asyncio
async def test_core_injection_is_idempotent_and_reversible() -> None:
    tool = server_module.mcp._tool_manager.get_tool("benthic_discover")
    assert tool is not None

    server_module._apply_core("- rule one", "pointer")
    server_module._apply_core("- rule one", "pointer")
    assert tool.description.count("rule one") == 1

    server_module._apply_core("", "pointer")
    assert "rule one" not in tool.description
    assert "Core access rules" not in tool.description
    server_module._applied.clear()


def test_query_and_join_pointers_are_injected(catalog: Catalog, settings: Settings) -> None:
    server_module._service = FakeService(catalog, settings)
    try:
        server_module._apply_core("- a rule", server_module.PLAYBOOK_POINTER)
        for name in ("benthic_query", "benthic_join", "benthic_rpc"):
            tool = server_module.mcp._tool_manager.get_tool(name)
            assert tool is not None
            assert "benthic_playbook" in tool.description
            assert "benthic_report" in tool.description
    finally:
        server_module._service = None
        server_module._applied.clear()


@pytest.mark.asyncio
async def test_lists_expected_tools(catalog: Catalog, settings: Settings) -> None:
    server_module._service = FakeService(catalog, settings)
    try:
        async with Client(server_module.mcp, raise_exceptions=True) as client:
            tools = (await client.list_tools()).tools
    finally:
        server_module._service = None

    assert "benthic_discover" in (server_module.mcp.instructions or "")
    assert "never invent a join" in (server_module.mcp.instructions or "")
    assert {tool.name for tool in tools} == {
        "benthic_discover",
        "benthic_playbook",
        "benthic_query",
        "benthic_join",
        "benthic_rpc",
        "benthic_report",
    }
    query_tool = next(tool for tool in tools if tool.name == "benthic_query")
    join_tool = next(tool for tool in tools if tool.name == "benthic_join")
    rpc_tool = next(tool for tool in tools if tool.name == "benthic_rpc")
    playbook_tool = next(tool for tool in tools if tool.name == "benthic_playbook")
    assert query_tool.input_schema["type"] == "object"
    assert set(query_tool.input_schema["properties"]) >= {"question", "source", "metrics"}
    assert set(join_tool.input_schema["properties"]) >= {
        "left_source",
        "right_source",
        "left_column",
        "right_column",
    }
    assert "operation" in rpc_tool.input_schema["properties"]
    assert rpc_tool.input_schema["properties"]["operation"]["enum"] == [
        "find_district",
        "districts_in_bbox",
        "nonprofits_nearby",
    ]
    assert set(playbook_tool.input_schema["properties"]) == {"dataset"}
    assert "$defs" not in query_tool.input_schema
    assert "$defs" not in join_tool.input_schema
    assert "$defs" not in rpc_tool.input_schema
    assert len(json.dumps(query_tool.input_schema)) < 1500
    assert len(json.dumps(join_tool.input_schema)) < 1600
    assert len(json.dumps(rpc_tool.input_schema)) < 1500


@pytest.mark.asyncio
async def test_tool_results_include_text_and_structured_content(catalog: Catalog, settings: Settings) -> None:
    fake = FakeService(catalog, settings)
    server_module._service = fake
    try:
        async with Client(server_module.mcp, raise_exceptions=True) as client:
            result = await client.call_tool("benthic_discover", {"query": "uei"})
            rpc_result = await client.call_tool(
                "benthic_rpc",
                {"operation": "find_district", "lat": 43.1, "lon": -70.2},
            )
    finally:
        server_module._service = None

    assert result.structured_content == {
        "query": "uei",
        "relations": [],
        "join_paths": [],
        "total_matches": 0,
        "more_available": False,
        "warnings": [],
    }
    assert result.content and result.content[0].type == "text"
    assert isinstance(rpc_result.structured_content, dict)
    assert rpc_result.structured_content["rows"] == [{"district_id": "ME-1"}]


@pytest.mark.asyncio
async def test_rpc_rejects_operation_outside_the_allowlist(catalog: Catalog, settings: Settings) -> None:
    server_module._service = FakeService(catalog, settings)
    try:
        async with Client(server_module.mcp, raise_exceptions=True) as client:
            result = await client.call_tool("benthic_rpc", {"operation": "drop_tables", "lat": 1.0, "lon": 1.0})
    finally:
        server_module._service = None

    assert result.is_error
    text = "".join(block.text for block in result.content if isinstance(block, TextContent))
    assert "nonprofits_nearby" in text


@pytest.mark.asyncio
async def test_playbook_serves_signed_facts_only(catalog: Catalog, settings: Settings) -> None:
    server_module._service = FakeService(catalog, settings)
    try:
        async with Client(server_module.mcp, raise_exceptions=True) as client:
            index = await client.call_tool("benthic_playbook", {})
            guide = await client.call_tool("benthic_playbook", {"dataset": "usp_cl"})
            missing = await client.call_tool("benthic_playbook", {"dataset": "nope"})
    finally:
        server_module._service = None

    datasets = {entry["dataset"] for entry in index.structured_content["datasets"]}
    assert datasets == {"usaspending", "samer", "irs_ng", "up_cdmaps", "usp_cl"}
    guide = guide.structured_content["datasets"][0]
    # The signed catalog joins all_entities to usp_cl.legislator_terms, so the guide must carry that
    # recipe with its partial reliability rather than claiming there is no path.
    assert guide["dataset"] == "usp_cl"
    assert [
        (recipe["left_source"], recipe["left_column"], recipe["right_source"], recipe["reliability"])
        for recipe in guide["join_recipes"]
    ] == [("usaspending.all_entities", "congressional_district", "usp_cl.legislator_terms", "partial")]
    assert "Unknown dataset" in str(missing)


@pytest.mark.asyncio
async def test_tool_calls_are_traced_without_argument_values(catalog: Catalog, settings: Settings) -> None:
    fake = FakeService(catalog, settings)
    server_module._service = fake
    try:
        async with Client(server_module.mcp, raise_exceptions=True) as client:
            await client.call_tool("benthic_discover", {"query": "secret-organization-name"})
            await client.call_tool("benthic_rpc", {"operation": "find_district", "lat": 43.1, "lon": -70.2})
    finally:
        server_module._service = None

    entries = fake.trace_store.entries()
    assert [entry.tool for entry in entries] == ["discover", "rpc"]
    assert fake.trace_store.log_path.is_file()
    logged = fake.trace_store.log_path.read_text(encoding="utf-8")
    assert "secret-organization-name" not in logged
    assert entries[1].sources == ["rpc:find_district"]


@pytest.mark.asyncio
async def test_report_stores_a_pending_lesson(catalog: Catalog, settings: Settings) -> None:
    fake = FakeService(catalog, settings)
    server_module._service = fake
    try:
        async with Client(server_module.mcp, raise_exceptions=True) as client:
            first = await client.call_tool(
                "benthic_report",
                {
                    "symptom": "Used a current-only view for a historical question",
                    "lesson": "Use `usp_cl.legislator_terms` with term bounds for historical questions.",
                    "dataset": "usp_cl",
                    "question_summary": "who represented CA-5 in 2019",
                },
            )
            second = await client.call_tool(
                "benthic_report",
                {
                    "symptom": "Used a current-only view for a historical question",
                    "lesson": "Use `usp_cl.legislator_terms` with term bounds for historical questions.",
                    "dataset": "usp_cl",
                },
            )
    finally:
        server_module._service = None

    records = fake.lesson_store.all()
    assert len(records) == 1
    assert records[0].status == "pending"
    assert records[0].occurrences == 2
    assert records[0].question_summary is None
    assert records[0].question_ref
    assert first.structured_content["status"] == "pending"
    assert second.structured_content["similar_pending"] == 1


@pytest.mark.asyncio
async def test_report_never_refuses_service_but_flags_unknown_names(catalog: Catalog, settings: Settings) -> None:
    fake = FakeService(catalog, settings)
    server_module._service = fake
    try:
        async with Client(server_module.mcp, raise_exceptions=True) as client:
            result = await client.call_tool(
                "benthic_report",
                {
                    "symptom": "guessed a relation that does not exist",
                    "lesson": "Use `usaspending.agency` because it looked right.",
                    "dataset": "not_a_dataset",
                    "relation": "not_a_relation",
                },
            )
    finally:
        server_module._service = None

    record = fake.lesson_store.all()[0]
    assert record.dataset is None
    assert record.relation is None
    warnings = result.structured_content["warnings"]
    assert any("Unknown dataset" in warning for warning in warnings)
    assert any("not in the signed catalog" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_http_app_requires_token_and_allows_exact_origin(settings: Any) -> None:
    token = "t" * 48
    http_settings = replace(
        settings,
        mcp_bearer_token=token,
        mcp_allowed_hosts=("127.0.0.1:8082",),
        mcp_allowed_origins=("http://192.168.10.222:8081",),
    )
    app = server_module.create_http_app(http_settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8082",
        ) as client:
            missing = await client.get("/health")
            wrong = await client.get("/health", headers={"Authorization": "Bearer wrong"})
            allowed = await client.get(
                "/health",
                headers={"Authorization": f"Bearer {token}", "Origin": "http://192.168.10.222:8081"},
            )
            preflight = await client.options(
                "/mcp",
                headers={
                    "Origin": "http://192.168.10.222:8081",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type,mcp-protocol-version",
                },
            )
            denied_origin = await client.get(
                "/health",
                headers={"Authorization": f"Bearer {token}", "Origin": "http://example.com"},
            )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://192.168.10.222:8081"
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://192.168.10.222:8081"
    assert denied_origin.status_code == 200
    assert "access-control-allow-origin" not in denied_origin.headers
