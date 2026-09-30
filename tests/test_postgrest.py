from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from benthic_mcp.catalog import Catalog
from benthic_mcp.errors import BenthicMCPError, QueryValidationError, UpstreamError
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
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-4/5"})
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
    assert result.matched_rows == 5
    assert [int(request.url.params.get("offset", 0)) for request in requests if request.method == "GET"] == [0, 2, 4]


@pytest.mark.asyncio
async def test_fetch_complete_rejects_scan_over_cap(settings: Any, catalog: Catalog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            # No count, so this is the path where the scan has to discover the cap by running out
            # of it. `test_fetch_complete_refuses_on_the_count_alone` covers the other one.
            return httpx.Response(200)
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
    assert result.matched_rows is None


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


@pytest.mark.asyncio
async def test_fetch_complete_refuses_to_page_a_relation_with_no_stable_order(settings: Any, catalog: Catalog) -> None:
    """`fetch_complete` exists to produce a set reliable enough to aggregate. It pages with `offset`,
    which is only meaningful against a total order.

    The order comes from the relation's primary key, and 63 of the 99 queryable relations in the
    signed catalog declare none - the fallback is `source.order`, which `build_single_query` never
    sets on any of its three `RelationSource` constructions. Those relations therefore page with no
    `ORDER BY` at all, and PostgREST guarantees nothing about row order across requests: a row can
    be returned twice or skipped entirely between page one and page two. The sum is then quietly
    wrong on the one path whose contract is that it is exact.

    The single-page case is safe and stays allowed - no offset is used, so order is irrelevant. Only
    a scan that actually needs a second page without a stable order is refused.
    """
    definition = catalog.resolve_relation("usaspending", "all_entities")
    without_key = replace(definition, primary_key=())
    assert without_key.primary_key == (), "the fixture relation must have no primary key for this to test anything"

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200)
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(offset, offset + limit)])

    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BenthicMCPError, match="no primary key"):
            await PostgrestTransport(small_settings, client).fetch_complete(
                RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
                without_key,
            )

    # Refused after one full page, so it never issued the unsafe second request.
    pages = [request for request in requests if request.method == "GET"]
    assert len(pages) == 1, f"issued {len(pages)} page requests; the unsafe one should never be sent"
    assert "order" not in pages[0].url.params, "paging without ORDER BY is the defect, so it must not happen"


@pytest.mark.asyncio
async def test_fetch_complete_allows_a_single_page_when_no_stable_order_exists(settings: Any, catalog: Catalog) -> None:
    """The counterpart: a result that fits in one page needs no offset and no order.

    Refusing here would block small relations outright, and there is nothing unsafe about it.
    """
    definition = replace(catalog.resolve_relation("usaspending", "all_entities"), primary_key=())

    total = 5

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": f"0-{total - 1}/{total}"})
        limit = int(request.url.params["limit"])
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(offset, min(offset + limit, total))])

    small_settings = replace(settings, max_rows=100, aggregate_scan_limit=1000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(small_settings, client).fetch_complete(
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            definition,
        )

    assert len(result.rows) == 5
    assert not result.truncated


@pytest.mark.asyncio
async def test_fetch_complete_asks_for_the_count_without_transferring_a_row(settings: Any, catalog: Catalog) -> None:
    """A refusal used to cost the whole page budget to find out it was going to refuse.

    Ten sequential pages and 1.6MB were pulled across the wire, aggregated nowhere, and dropped, so
    the caller was told the query was too big only after paying for the rows that proved it. The
    count that answers the same question is one HEAD and a header, so the request has to be a HEAD
    and it has to carry no `limit`, or the saving is only half of what it looks like.
    """
    requests: list[httpx.Request] = []
    source_rows = [{"uei": str(index)} for index in range(5)]
    count_body_reads: list[bytes] = []

    class _CountBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"[", b"row", b"]"):
                count_body_reads.append(chunk)
                yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            # The count arrives in a header, so the rows that would follow it are never read. A
            # streamed body records its own consumption, which is what makes that checkable.
            return httpx.Response(200, headers={"Content-Range": "0-4/5"}, stream=_CountBody())
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=source_rows[offset : offset + limit])

    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(small_settings, client).fetch_complete(
            RelationSource(
                alias="awards",
                dataset="usaspending",
                relation="all_entities",
                select=["uei"],
                filters=[FilterSpec(column="state", operator=FilterOperator.EQ, value="ME")],
            ),
            catalog.resolve_relation("usaspending", "all_entities"),
        )

    assert result.rows == source_rows
    assert result.matched_rows == 5
    assert count_body_reads == [], "the count is a header; the rows behind it are never transferred"
    count = requests[0]
    assert count.method == "HEAD", "a GET would transfer the rows it is trying not to read"
    assert count.headers["prefer"] == "count=exact"
    assert count.headers["range"] == "0-0"
    assert "limit" not in count.url.params, "a count over 0 rows is not a 0-row request"
    # The count has to describe the rows the scan would page, so it carries the scan's own filters.
    assert count.url.params["state"] == "eq.ME"
    assert count.url.params["select"] == "uei"


@pytest.mark.asyncio
async def test_fetch_complete_refuses_on_the_count_alone(settings: Any, catalog: Catalog) -> None:
    """A count above the cap is the answer, so no page of it needs to be fetched to be believed.

    `congressional_district=eq.03` matches 1416153 rows of usaspending.all_entities against a limit
    of 10000. Paging that to find out costs 10 requests and about 1.6MB for a number the caller then
    has to be told anyway, and the rows it fetched are never read.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-1416152/1416153"})
        raise AssertionError(f"a scan over the cap must not be paged, but {request.method} was sent")

    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(small_settings, client).fetch_complete(
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            catalog.resolve_relation("usaspending", "all_entities"),
        )

    assert result.truncated
    assert result.matched_rows == 1416153
    assert result.rows == []
    assert [request.method for request in requests] == ["HEAD"]
    assert result.request_url.startswith("https://benthic.io/ngopen/usaspending/all_entities")


@pytest.mark.asyncio
async def test_fetch_complete_refuses_from_the_manifest_estimate_without_asking(
    settings: Any, catalog: Catalog
) -> None:
    """The manifest already carries the row count, so an unfiltered scan over the cap must not be
    spent finding out.

    usaspending.prime_awards is 183M rows across 192GB and declares `row_count_estimate`. An
    aggregate over it with no filter is over the cap by a factor of 18000, and the count HEAD that
    discovers this cost 26s once, then 74s on a filtered sibling. The estimate is a different kind
    of evidence: it is in the signed catalog, costs nothing to read, and is already larger than the
    cap before a socket is opened.

    Only sound with no filters. An estimate bounds the filtered count from above, so with a filter
    present it cannot establish that the cap is exceeded - that is what
    `test_fetch_complete_still_counts_when_a_filter_could_beat_the_estimate` covers.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError(f"the manifest already answers this, but {request.method} was sent")

    huge = replace(catalog.resolve_relation("usaspending", "all_entities"), row_count_estimate=182995664)
    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(small_settings, client).fetch_complete(
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            huge,
        )

    assert requests == [], "a refusal known from the manifest must cost no upstream request"
    assert result.truncated
    assert result.rows == []
    assert result.matched_rows == 182995664


@pytest.mark.asyncio
async def test_fetch_complete_still_counts_when_a_filter_could_beat_the_estimate(
    settings: Any, catalog: Catalog
) -> None:
    """The estimate is an upper bound, so a filter has to be measured rather than assumed away.

    A 183M-row relation filtered to one congressional district can match 1,416,153 rows, and some
    filters match fewer than the cap. Refusing on the unfiltered estimate would strand those, and
    the estimate cannot say which is which - that is the same reason the sorted-key walk cannot
    establish output size.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-4/5"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(5)][offset : offset + limit])

    huge = replace(catalog.resolve_relation("usaspending", "all_entities"), row_count_estimate=182995664)
    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(small_settings, client).fetch_complete(
            RelationSource(
                alias="awards",
                dataset="usaspending",
                relation="all_entities",
                filters=[FilterSpec(column="congressional_district", operator=FilterOperator.EQ, value="03")],
            ),
            huge,
        )

    assert [request.method for request in requests][0] == "HEAD", "a filter has to be measured"
    assert result.rows != [], "a filter that brings 183M rows under the cap must still be answered"
    assert result.matched_rows == 5


@pytest.mark.asyncio
async def test_fetch_complete_ignores_an_estimate_under_the_cap(settings: Any, catalog: Catalog) -> None:
    """An estimate below the cap says nothing about whether the filters pass, so nothing changes."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-4/5"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(5)][offset : offset + limit])

    modest = replace(catalog.resolve_relation("usaspending", "all_entities"), row_count_estimate=8)
    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(small_settings, client).fetch_complete(
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            modest,
        )

    assert result.rows != []
    assert result.matched_rows == 5


@pytest.mark.asyncio
async def test_fetch_complete_ignores_a_never_analyzed_estimate(settings: Any, catalog: Catalog) -> None:
    """reltuples is -1 for a table that has never been ANALYZEd, and -1 is not a row count.

    bdp/tools/introspect.py drops the estimate when reltuples is 0, so a negative one can reach here
    after that fix. -1 is below any cap, so it refuses to short-circuit - but it must not be
    reported as a matched count either, or a caller would be told the scan matched -1 rows.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-4/5"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(5)][offset : offset + limit])

    unanalyzed = replace(catalog.resolve_relation("usaspending", "all_entities"), row_count_estimate=-1)
    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(small_settings, client).fetch_complete(
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            unanalyzed,
        )

    assert result.matched_rows == 5, "the count is measured, not taken from the estimate"
    assert result.rows != []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [405, 500, 404])
async def test_fetch_complete_pages_when_the_count_fails(settings: Any, catalog: Catalog, status: int) -> None:
    """The count is an optimisation, so it can fail without the query failing with it.

    A PostgREST build that does not answer HEAD, a 500 from the count, or a row count it declines to
    report all have to leave the scan working exactly as it was, because the alternative is a
    version of this server that cannot run any aggregate against any relation.
    """
    requests: list[httpx.Request] = []
    source_rows = [{"uei": str(index)} for index in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(status, json={"message": "no count here"})
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
    assert result.matched_rows is None
    assert [int(request.url.params.get("offset", 0)) for request in requests if request.method == "GET"] == [0, 2, 4]


@pytest.mark.asyncio
async def test_fetch_complete_pages_when_the_count_is_not_reported(settings: Any, catalog: Catalog) -> None:
    """A 200 with no usable `Content-Range` is a count that did not come back, not a count of zero.

    Reading the missing total as 0 would refuse every aggregate on the relation, and reading it as
    absent is the only safe reading.
    """
    source_rows = [{"uei": str(index)} for index in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
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
    assert result.matched_rows is None


@pytest.mark.asyncio
async def test_fetch_complete_pages_when_the_count_request_raises(settings: Any, catalog: Catalog) -> None:
    source_rows = [{"uei": str(index)} for index in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            raise httpx.ReadTimeout("count timed out", request=request)
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
    assert result.matched_rows is None


@pytest.mark.asyncio
async def test_count_matching_reads_the_total_out_of_an_unsatisfiable_range(settings: Any, catalog: Catalog) -> None:
    """PostgREST answers `*/<total>` once the requested range is past the end of the result.

    An empty relation is the case where the range 0-0 is unsatisfiable, so it is the case where the
    header stops being `0-0/5` shaped, and an aggregate over an empty relation is a real answer.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"Content-Range": "*/0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        count = await PostgrestTransport(settings, client).count_matching(
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            catalog.resolve_relation("usaspending", "all_entities"),
        )

    assert count is not None
    assert count.total == 0
    assert requests[0].method == "HEAD"


@pytest.mark.asyncio
async def test_a_count_request_never_carries_an_order(settings: Any, catalog: Catalog) -> None:
    """A count is "how many rows match", which an ORDER BY cannot change, so asking for one is pure
    cost - and on a large relation it is the dominant cost.

    Measured live against usaspending.all_entities (17,884,243 rows):
        filter only, no order    2.09s
        filter + order=entity_id 3.35s
        order=entity_id, no filter 26.10s

    `fetch_complete` counts the `scan_source`, which carries the primary-key order the paging loop
    needs, so every aggregate inherited the order. The unfiltered count went from about two seconds
    to twenty-six, which is longer than the entire turn budget a model is given.

    The order exists to make `offset` deterministic across pages. A HEAD fetches no rows, pages
    nothing, and needs no order - so stripping it costs nothing and cannot change the count.
    """
    definition = catalog.resolve_relation("usaspending", "all_entities")
    assert definition.primary_key, "the fixture must declare a primary key for the order to be added"

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-1416152/1416153"})
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        count = await PostgrestTransport(settings, client).count_matching(
            RelationSource(
                alias="awards",
                dataset="usaspending",
                relation="all_entities",
                order=[SourceOrder(column=column) for column in definition.primary_key],
            ),
            definition,
        )

    heads = [request for request in requests if request.method == "HEAD"]
    assert heads, "no count request was issued"
    assert "order" not in heads[0].url.params, (
        f"the count request carried {heads[0].url.params['order']}; on a large relation that is "
        f"the whole cost of the count"
    )
    # The count is still the one the scan would see.
    assert count is not None and count.total == 1416153


@pytest.mark.asyncio
async def test_a_page_reports_how_many_rows_the_filters_matched_in_total(settings: Any, catalog: Any) -> None:
    """A truncated page says only `truncated: true`, so a caller cannot tell 100 rows out of 100
    from 100 rows out of 1.4 million, and has no way to know whether paging would ever finish.

    Found by driving the live interface. A session asked for entities in MA district 03 - 1,416,153
    rows - received 100, saw `truncated: true` with `next_offset: null`, and spent every remaining
    turn querying again instead of answering with what it had and a caveat. The server knew the
    exact figure: it had just asked for it. The number was used only to build refusal text and was
    thrown away on success.

    This is the same defect as the count the scan already pays for, on the path that succeeds. The
    model cannot budget turns without knowing the size of what it is looking at.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-1416152/1416153"})
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(limit)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PostgrestTransport(settings, client).fetch(
            RelationSource(alias="s", dataset="usaspending", relation="all_entities"),
            catalog.resolve_relation("usaspending", "all_entities"),
        )

    assert result.truncated
    assert result.matched_rows == 1416153, (
        f"a truncated page must say how many rows exist, not only that it cut off; got {result.matched_rows}"
    )
