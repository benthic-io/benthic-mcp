import asyncio
import hashlib
from typing import Literal

import httpx

from benthic_mcp.bdp import BdpRepository
from benthic_mcp.catalog import Catalog
from benthic_mcp.config import Settings
from benthic_mcp.models import (
    DiscoverResult,
    QueryRequest,
    QueryResult,
    ReportResult,
    RpcRequest,
    RpcResult,
)
from benthic_mcp.playbook import (
    LessonRecord,
    Playbook,
    VerifyReport,
    build_path_result,
    build_result,
    lesson_id,
    lesson_is_grounded,
    load_playbook,
    relation_hints,
    render_core,
    render_instructions,
    staleness,
    verify,
)
from benthic_mcp.postgrest import PostgrestTransport
from benthic_mcp.query import QueryService
from benthic_mcp.rpc import RpcService
from benthic_mcp.seed import seed_playbook
from benthic_mcp.trace import LessonStore, TraceStore

__all__ = ["BenthicService", "PlaybookRuntime", "seed_playbook"]


class PlaybookRuntime:
    """Verified playbook plus the catalog it was verified against, loaded once per process."""

    def __init__(
        self,
        playbook: Playbook | None,
        catalog: Catalog,
        status: str,
        report: VerifyReport,
        warnings: list[str],
        token_budget: int,
    ) -> None:
        self.playbook = playbook
        self.catalog = catalog
        self.status = status
        self.report = report
        self.warnings = warnings
        self.core = render_core(playbook, catalog, token_budget)

    def result(self, dataset: str | None):
        return build_result(self.playbook, self.catalog, self.status, dataset, list(self.warnings))

    def path_result(self, from_relation: str, to_relation: str, max_hops: int = 2):
        return build_path_result(self.catalog, self.status, from_relation, to_relation, max_hops, list(self.warnings))

    def instructions(self) -> str:
        return render_instructions(self.core, self.catalog)


def _collection_name(catalog: Catalog, fallback: str) -> str:
    names = list(catalog.collection_definitions)
    return names[0] if len(names) == 1 else fallback


def _build_runtime(settings: Settings, catalog: Catalog) -> PlaybookRuntime:
    warnings: list[str] = []
    if settings.playbook_mode == "off":
        return PlaybookRuntime(None, catalog, "disabled", VerifyReport(), warnings, settings.playbook_token_budget)

    if settings.playbook_mode == "active":
        path = settings.playbook_path
        playbook, status = load_playbook(path) if path else (None, "unavailable")
        if playbook is None:
            warnings.append(f"No promoted playbook at {path}; serving the curated seed instead.")
            playbook, status = seed_playbook(), "seed"
    else:
        playbook, status = seed_playbook(), "seed"

    playbook = playbook.model_copy(update={"collection": _collection_name(catalog, playbook.collection)})
    verified, report = verify(playbook, catalog)
    warnings.extend(report.notes())

    stale = staleness(verified, catalog)
    if stale:
        # Signed facts are still served; only the generated prose is withheld.
        verified = verified.model_copy(update={"datasets": {}, "relations": {}, "lessons": []})
        return PlaybookRuntime(verified, catalog, "stale", report, [stale, *warnings], settings.playbook_token_budget)

    return PlaybookRuntime(verified, catalog, status, report, warnings, settings.playbook_token_budget)


class BenthicService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        )
        self.repository = BdpRepository(settings, self.client)
        self.transport = PostgrestTransport(settings, self.client)
        self.query_service = QueryService(settings, self.repository, self.transport)
        self.rpc_service = RpcService(settings, self.client)
        self.trace_store = TraceStore(
            settings.cache_dir / "traces",
            enabled=settings.trace_enabled,
            retention_days=settings.trace_retention_days,
            include_text=settings.trace_include_text,
        )
        self.lesson_store = LessonStore(settings.cache_dir / "lessons")
        self._playbook: PlaybookRuntime | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def playbook(self) -> PlaybookRuntime:
        if self._playbook is not None:
            return self._playbook
        snapshot = await self.repository.load()
        self._playbook = _build_runtime(self.settings, Catalog(snapshot))
        return self._playbook

    async def record_lesson(
        self,
        symptom: str,
        lesson: str,
        question_summary: str = "",
        dataset: str | None = None,
        relation: str | None = None,
        confidence: Literal["high", "medium", "low"] = "medium",
    ) -> ReportResult:
        """Store one author-reported lesson. Shared by the benthic_report tool and the harness
        reflector, so both go through identical validation, grounding, and merge behaviour."""
        runtime = await self.playbook()
        catalog = runtime.catalog
        warnings: list[str] = []

        if dataset is not None and dataset not in catalog.datasets:
            warnings.append(f"Unknown dataset {dataset}; the lesson was recorded without one.")
            dataset = None
        if relation is not None and tuple(relation.split(".", 1)) not in catalog.relations:
            warnings.append(f"Unknown relation {relation}; the lesson was recorded without one.")
            relation = None

        record = LessonRecord(
            lesson_id=lesson_id(),
            dataset=dataset,
            relation=relation,
            symptom=symptom.strip()[:500],
            lesson=lesson.strip()[:500],
            confidence=confidence,
            # A voluntary report need not carry a question, and hashing the empty string would
            # produce a plausible-looking reference that resolves to nothing. Absence stays visible.
            question_ref=(
                hashlib.sha256(question_summary.encode("utf-8")).hexdigest()[:16] if question_summary else ""
            ),
            question_summary=question_summary.strip()[:300] if self.settings.trace_include_text else None,
            catalog_fingerprint=runtime.catalog.fingerprint(),
            evidence=self.trace_store.recent_summary(12),
        )
        merged = await asyncio.to_thread(self.lesson_store.merge, record)
        if not lesson_is_grounded(merged, catalog):
            warnings.append(
                "The lesson text references names that are not in the signed catalog; "
                "it will be dropped during consolidation unless reworded."
            )
        return ReportResult(
            recorded=True,
            lesson_id=merged.lesson_id,
            status=merged.status,
            similar_pending=len(
                await asyncio.to_thread(self.lesson_store.similar_pending, merged.symptom, merged.dataset)
            ),
            review_hint="Recorded as pending. It reaches future sessions only after consolidation "
            "and an offline A/B evaluation pass.",
            dataset_slice=runtime.result(dataset).datasets[0] if dataset is not None else None,
            warnings=warnings,
        )

    async def discover(
        self,
        query: str = "",
        dataset: str | None = None,
        relation: str | None = None,
        limit: int = 6,
        detail: str = "summary",
    ) -> DiscoverResult:
        runtime = await self.playbook()
        snapshot = await self.repository.load()
        return Catalog(snapshot, relation_hints=relation_hints(runtime.playbook)).discover(
            query=query,
            dataset=dataset,
            relation=relation,
            detail=detail,
            limit=min(limit, self.settings.max_rows),
        )

    async def query(self, request: QueryRequest) -> QueryResult:
        return await self.query_service.execute(request)

    async def rpc(self, request: RpcRequest) -> RpcResult:
        snapshot = await self.repository.load()
        return await self.rpc_service.execute(Catalog(snapshot), request)
