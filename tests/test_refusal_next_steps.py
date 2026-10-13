"""A refusal must point at the next step, or it dead-ends the caller.

`check_pipeline_provenance.py`'s cousin problem: a message that names a failure and nothing else
costs the user the turns they spend guessing what to do. These contracts assert the high-frequency
dead-ends from a full audit of every refusal string carry a concrete next step.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from benthic_mcp.bdp import BdpRepository
from benthic_mcp.catalog import Catalog
from benthic_mcp.errors import QueryValidationError
from benthic_mcp.models import (
    AggregateFunction,
    AggregateSpec,
    FilterOperator,
    FilterSpec,
    QueryRequest,
    RelationSource,
)
from benthic_mcp.postgrest import PostgrestTransport
from benthic_mcp.query import QueryService, _numeric, build_single_query


def test_a_missing_relation_points_at_discover(catalog: Catalog) -> None:
    """`Relation X is not in the signed BDP manifest` told the caller it failed and nothing else.

    The discover-side copy already says "call discover without a relation to see the signed relations"
    (catalog.py:298-301). The query-side copy did not.
    """
    with pytest.raises(QueryValidationError, match="benthic_discover"):
        catalog.resolve_relation("usaspending", "no_such_relation")


def test_a_nonqueryable_relation_points_at_discover(catalog: Catalog) -> None:
    with pytest.raises(QueryValidationError, match="benthic_discover"):
        catalog.resolve_relation("usaspending", "lineage")


def test_a_non_numeric_aggregate_offers_the_exit(catalog: Catalog) -> None:
    """`Aggregate X requires numeric values` states the failure and gives no way out.

    The exit is either to count rows instead of summing them, or to sum a column that is numeric. The
    message said neither, so a caller hitting it had to guess which one was meant.
    """
    with pytest.raises(QueryValidationError, match="count"):
        _numeric("not a number", "sum")


def _query_client(bdp_documents: dict[str, Any], rows_by_path: dict[str, list[dict[str, Any]]]) -> httpx.AsyncClient:
    """A QueryService whose transport serves the manifest and canned rows, the way test_query.py
    drives the same boundary: the contract has to cross what the model crosses, or a unit test of
    the private raiser passes while the served message says something else."""

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.url.path in rows_by_path:
            return httpx.Response(200, json=rows_by_path[request.url.path])
        raise AssertionError(f"no rows canned for {request.method} {request.url.path}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_type_mismatch_names_the_column(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """`Value 'notayear' does not match column type integer` names the value and the type but not
    the column.

    A query with several filters of the same type gives the caller no way to tell which one is
    wrong, so it re-issues the call with each filter changed in turn. The raiser holds the column
    definition; the name costs nothing.
    """
    client = _query_client(bdp_documents, {"/ngopen/usaspending/overall_totals": [{"fiscal_year": 2020}]})
    service = QueryService(settings, BdpRepository(settings, client), PostgrestTransport(settings, client))
    request = QueryRequest(
        question="totals",
        sources=[
            RelationSource(
                alias="s",
                dataset="usaspending",
                relation="overall_totals",
                filters=[FilterSpec(column="fiscal_year", operator=FilterOperator.EQ, value="notayear")],
            )
        ],
    )
    with pytest.raises(QueryValidationError, match="fiscal_year"):
        await service.execute(request)
    await client.aclose()


def test_a_128_column_refusal_names_the_count() -> None:
    """`A single-source query can select at most 128 columns` states the limit but not how far
    over the caller is.

    The count is in hand at the raise site, and without it the caller cannot tell a 129-column
    select from a select-everything one - two trims of very different size.
    """
    with pytest.raises(QueryValidationError, match="129"):
        build_single_query(
            question="everything",
            dataset="usaspending",
            relation="all_entities",
            select=[f"col{index}" for index in range(129)],
            where=None,
            group_by=None,
            metrics=None,
            having=None,
            order=None,
            limit=100,
            offset=0,
        )


@pytest.mark.asyncio
async def test_a_group_by_refusal_names_the_column(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """`A group_by column is not present in the joined result` names neither the column nor the
    rule.

    The caller grouped by something the join did not carry - usually a misspelling - and the
    message gives it no pointer to which key is wrong, so it re-issues the call with each group
    key changed in turn. The missing names are in hand at the raise site.
    """
    client = _query_client(bdp_documents, {"/ngopen/usaspending/all_entities": [{"uei": "A"}]})
    service = QueryService(settings, BdpRepository(settings, client), PostgrestTransport(settings, client))
    request = QueryRequest(
        question="count by nope",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities")],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column=None, alias="n")],
        group_by=["nope"],
    )
    with pytest.raises(QueryValidationError, match="nope"):
        await service.execute(request)
    await client.aclose()
