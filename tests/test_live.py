import os
from dataclasses import replace
from pathlib import Path

import pytest

from benthic_mcp.catalog import Catalog
from benthic_mcp.config import Settings
from benthic_mcp.models import FilterOperator, FilterSpec, JoinSpec, QueryRequest, RelationSource, Reliability
from benthic_mcp.query import build_single_query, unqualify_result
from benthic_mcp.service import BenthicService

pytestmark = pytest.mark.live


def _live_enabled() -> bool:
    return os.environ.get("BENTHIC_LIVE_TESTS") == "1"


@pytest.mark.asyncio
async def test_live_signed_catalog(tmp_path: Path) -> None:
    if not _live_enabled():
        pytest.skip("set BENTHIC_LIVE_TESTS=1 to run live Benthic tests")
    settings = replace(Settings.from_env(), cache_dir=tmp_path / "cache")
    service = BenthicService(settings)
    try:
        snapshot = await service.repository.load()
        result = Catalog(snapshot).discover(query="uei", limit=5)
        assert result.query == "uei"
        assert result.relations
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_live_reliable_identifier_join(tmp_path: Path) -> None:
    if not _live_enabled():
        pytest.skip("set BENTHIC_LIVE_TESTS=1 to run live Benthic tests")
    settings = replace(Settings.from_env(), cache_dir=tmp_path / "cache")
    service = BenthicService(settings)
    try:
        seed = unqualify_result(
            await service.query(
                build_single_query(
                    question="Find one SAM UEI",
                    dataset="samer",
                    relation="sam_registrations",
                    select=["uei"],
                    where=None,
                    group_by=None,
                    metrics=None,
                    having=None,
                    order=None,
                    limit=1,
                    offset=0,
                )
            )
        )
        assert seed.rows
        uei = seed.rows[0]["uei"]
        result = await service.query(
            QueryRequest(
                question="Show one reliable USAspending to SAM registration match",
                sources=[
                    RelationSource(
                        alias="awards",
                        dataset="usaspending",
                        relation="all_entities",
                        select=["uei"],
                        filters=[FilterSpec(column="uei", operator=FilterOperator.EQ, value=uei)],
                    ),
                    RelationSource(
                        alias="sam",
                        dataset="samer",
                        relation="sam_registrations",
                        select=["uei"],
                        filters=[FilterSpec(column="uei", operator=FilterOperator.EQ, value=uei)],
                    ),
                ],
                joins=[
                    JoinSpec(
                        left_alias="awards",
                        right_alias="sam",
                        left_column="uei",
                        right_column="uei",
                    )
                ],
                allowed_reliability=[Reliability.RELIABLE],
                limit=5,
            )
        )
        assert result.sources
        assert result.joins[0].reliability == Reliability.RELIABLE
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_live_2022_district_12_daily_award_aggregate(tmp_path: Path) -> None:
    if os.environ.get("BENTHIC_LIVE_REGRESSION") != "1":
        pytest.skip("set BENTHIC_LIVE_REGRESSION=1 to run the full award regression")
    settings = replace(
        Settings.from_env(),
        cache_dir=tmp_path / "cache",
        aggregate_scan_limit=2_000,
        request_timeout_seconds=30.0,
    )
    request = build_single_query(
        question="January 2022 district 12 organizations over 100000",
        dataset="usaspending",
        relation="prime_awards",
        select=None,
        where=[
            "action_date=gte.2022-01-01",
            "action_date=lte.2022-01-01",
            "recipient_congressional_district=eq.12",
        ],
        group_by=["recipient_uei", "recipient_name"],
        metrics=["total=sum:award_amount"],
        having=["total>100000"],
        order=["total:desc"],
        limit=20,
        offset=0,
    )
    service = BenthicService(settings)
    try:
        result = unqualify_result(await service.query(request))
        assert result.source_complete
        assert result.columns == ["recipient_uei", "recipient_name", "total"]
        assert all(row["total"] > 100000 for row in result.rows)
    finally:
        await service.close()
