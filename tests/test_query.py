import re
from dataclasses import replace
from typing import Any

import httpx
import pytest

from benthic_mcp.bdp import BdpRepository
from benthic_mcp.errors import QueryValidationError
from benthic_mcp.models import (
    AggregateFunction,
    AggregateSpec,
    FilterOperator,
    FilterSpec,
    HavingSpec,
    JoinMode,
    JoinSpec,
    OutputOrder,
    QueryRequest,
    RelationSource,
    Reliability,
)
from benthic_mcp.postgrest import PostgrestTransport
from benthic_mcp.query import QueryService, build_single_query, unqualify_result


def _query_client(bdp_documents: dict[str, Any], rows_by_path: dict[str, list[dict[str, Any]]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        path = request.url.path
        if path in rows_by_path:
            return httpx.Response(200, json=rows_by_path[path])
        return httpx.Response(404, json={"error": "not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_executes_signed_multi_dataset_query(settings: Any, bdp_documents: dict[str, Any]) -> None:
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [
                {"uei": "A", "name": "Recipient A", "state": "ME"},
                {"uei": "B", "name": "Recipient B", "state": "ME"},
            ],
            "/ngopen/samer/sam_registrations": [
                {"uei": "A", "name": "SAM A"},
                {"uei": "B", "name": "SAM B"},
            ],
        },
    )
    service = QueryService(
        settings,
        BdpRepository(settings, client),
        PostgrestTransport(settings, client),
    )
    request = QueryRequest(
        question="Show recipient registrations",
        sources=[
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            RelationSource(alias="sam", dataset="samer", relation="sam_registrations"),
        ],
        joins=[JoinSpec(left_alias="awards", right_alias="sam", left_column="uei", right_column="uei")],
        allowed_reliability=[Reliability.RELIABLE],
    )

    result = await service.execute(request)
    await client.aclose()

    assert result.row_count == 2
    assert result.rows[0]["awards.name"] == "Recipient A"
    assert result.rows[0]["sam.name"] == "SAM A"
    assert result.joins[0].reliability == Reliability.RELIABLE


def test_no_filter_example_the_server_advertises_is_one_it_would_refuse() -> None:
    """A message that shows a spelling the server then rejects costs the caller its turns.

    `_FILTER_SYNTAX` advertised `'column=in."a","b"'`. `_parse_filter` requires `json.loads` to accept
    what follows `in.`, and `json.loads('"a","b"')` raises `Extra data` - so the one example the server
    printed was the one it refused. The invalid-in refusal said "use a JSON array" without showing one.

    This is not hypothetical. `deadend_empty` failed in 33 consecutive cycles, and in two of them the
    model copied the advertised form verbatim and was refused for it: T6 of `20261007T200544` and T7 of
    `20261006T170851`, having already spent the turn before it on the same mistake.

    Every filter-shaped example in the syntax text and in the invalid-in refusal is extracted and fed
    back through the parser. If any of them would be refused, this fails - which is the property that
    matters, rather than a check that one particular string happens to be right today.
    """
    from benthic_mcp.query import _FILTER_SYNTAX, _parse_filter

    examples = re.findall(r"'([a-z_]+=[^']+)'", _FILTER_SYNTAX)
    assert examples, "the syntax text has to show examples for this to check anything"

    for example in examples:
        if "operator.value" in example or "operator" in example:
            continue
        _parse_filter(example)  # raises if the server would refuse what it advertises

    # And the refusal for a malformed `in` has to show a spelling that works, not just describe one.
    with pytest.raises(QueryValidationError) as caught:
        _parse_filter('uei=in."A","B"')
    assert 'in.["A","B"]' in str(caught.value), "the invalid-in refusal has to show a spelling that parses: " + str(
        caught.value
    )


@pytest.mark.asyncio
async def test_order_limit_and_offset_reach_the_database(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """`order=`, `limit=` and `offset=` have to be pushed to PostgREST, not applied in Python.

    `build_single_query` built its `RelationSource` with only alias, dataset, relation, select and
    filters, so all three kept their defaults and the transport never sent them. `_order_rows` then
    sorted one fixed page, which made "the largest X" a statement about whichever 100 rows came back.

    Measured on usaspending.prime_awards before the fix:

        order=[total_obligation:desc] limit=1  ->  2,698,943.00     what the server called the largest
        count(total_obligation > 2,698,943)    ->  2,374,098 rows are larger
        psql ORDER BY total_obligation DESC    ->  373,109,113,199.00

    Off by about 138,000x, on a probe set where the pass rate therefore never measured accuracy.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-0/2"})
        return httpx.Response(
            200, json=[{"total_obligation": 373109113199.0, "legal_business_name": "MULTIPLE RECIPIENTS"}]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(settings, BdpRepository(settings, client), PostgrestTransport(settings, client))
        result = await service.execute(
            build_single_query(
                question="largest total obligation",
                source="usaspending.all_entities",
                select=["legal_business_name", "total_obligation"],
                where=None,
                group_by=None,
                metrics=None,
                having=None,
                order=["total_obligation:desc"],
                limit=1,
                offset=0,
            )
        )

    sent = requests[-1].url.params
    assert sent.get("order") == "total_obligation.desc", (
        "the database must be asked to sort; sorting one page in Python cannot answer "
        f"'the largest X'. Request sent: {requests[-1].url}"
    )
    assert "limit" in sent, f"the request must carry a limit. Sent: {requests[-1].url}"
    assert result.rows[0]["s.total_obligation"] == 373109113199.0


@pytest.mark.asyncio
async def test_an_order_on_an_aggregate_alias_is_not_pushed_to_the_source(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """An aggregate alias is not a column, so it cannot be an ORDER BY on the source.

    `order=["mx:desc"]` over `metrics=["mx=max:total_obligation"]` sorts computed output. Pushing that
    to the source would ask PostgREST to order a column named `mx` that does not exist there. It has
    to stay a local sort, which is correct here precisely because the aggregate is computed by the
    database over every matching row.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-0/2"})
        return httpx.Response(200, json=[{"mx": 2.0}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(settings, BdpRepository(settings, client), PostgrestTransport(settings, client))
        await service.execute(
            build_single_query(
                question="largest",
                source="usaspending.all_entities",
                select=None,
                where=None,
                group_by=None,
                metrics=["mx=max:total_obligation"],
                having=None,
                order=["mx:desc"],
                limit=10,
                offset=0,
            )
        )

    # `fetch_complete` adds the primary-key order so paging is deterministic. That is correct and is
    # not what this contract is about: what must not happen is `mx` reaching the source.
    assert "mx" not in (requests[-1].url.params.get("order") or ""), (
        f"an aggregate alias must not become a source ORDER BY: {requests[-1].url}"
    )


@pytest.mark.asyncio
async def test_aggregates_and_orders_joined_rows(settings: Any, bdp_documents: dict[str, Any]) -> None:
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [
                {"uei": "A", "state": "ME"},
                {"uei": "B", "state": "ME"},
                {"uei": "C", "state": "VT"},
            ],
            "/ngopen/samer/sam_registrations": [
                {"uei": "A"},
                {"uei": "B"},
            ],
        },
    )
    service = QueryService(
        settings,
        BdpRepository(settings, client),
        PostgrestTransport(settings, client),
    )
    request = QueryRequest(
        question="Count registered recipients by state",
        sources=[
            RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
            RelationSource(alias="sam", dataset="samer", relation="sam_registrations"),
        ],
        joins=[
            JoinSpec(
                left_alias="awards",
                right_alias="sam",
                left_column="uei",
                right_column="uei",
                mode=JoinMode.LEFT,
            )
        ],
        group_by=["awards.state"],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, alias="recipient_count")],
        order=[OutputOrder(column="awards.state")],
    )

    result = await service.execute(request)
    await client.aclose()

    assert result.rows == [
        {"awards.state": "ME", "recipient_count": 2},
        {"awards.state": "VT", "recipient_count": 1},
    ]


@pytest.mark.asyncio
async def test_single_query_builds_filters_having_and_unqualified_output(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    source_rows = [
        {"uei": "A", "name": "A", "state": "ME", "total_obligation": 60000},
        {"uei": "A", "name": "A", "state": "ME", "total_obligation": 50000},
        {"uei": "B", "name": "B", "state": "VT", "total_obligation": 70000},
        {"uei": "B", "name": "B", "state": "VT", "total_obligation": 200},
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-3/4"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=source_rows[offset : offset + limit])

    request = build_single_query(
        question="Organizations over 100000",
        dataset="usaspending",
        relation="all_entities",
        select=None,
        where=["state=eq.ME"],
        group_by=["uei", "name", "state"],
        metrics=["total=sum:total_obligation"],
        having=["total>100000"],
        order=["total:desc"],
        limit=100,
        offset=0,
    )
    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings,
            BdpRepository(small_settings, client),
            PostgrestTransport(small_settings, client),
        )
        result = unqualify_result(await service.execute(request))

    assert result.source_complete
    assert result.rows == [{"uei": "A", "name": "A", "state": "ME", "total": 110000}]
    assert requests[0].url.params["state"] == "eq.ME"
    assert len(requests[0].url.params.get_list("state")) == 1
    assert len(requests) >= 2


@pytest.mark.asyncio
async def test_rejects_incomplete_aggregate_scan(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            # No count, so this is the scan that has to run out of its budget to discover the cap.
            return httpx.Response(200)
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(offset, offset + limit)])

    small_settings = replace(settings, max_rows=2, aggregate_scan_limit=3)
    request = QueryRequest(
        question="Count recipients",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities")],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, alias="recipient_count")],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings,
            BdpRepository(small_settings, client),
            PostgrestTransport(small_settings, client),
        )
        with pytest.raises(QueryValidationError, match="complete-scan limit") as caught:
            await service.execute(request)

    # With no count to go on, the cap is the only size the refusal can name, and it names it anyway.
    assert "More than 3 rows match" in str(caught.value)
    assert "usaspending.all_entities" in str(caught.value)


@pytest.mark.asyncio
async def test_an_over_cap_refusal_names_the_row_count_and_how_far_over_it_is(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """The refusal used to say only "narrow the filters", to a caller that had already narrowed.

    `congressional_district=eq.03` is a filter that looks narrow and matches 1416153 of the rows in
    usaspending.all_entities, 141 times the limit. Told only to narrow, a caller cannot tell that
    no amount of narrowing gets it under 10000, so it retries filters and gets told to narrow again.
    The count is the whole difference between those two situations, so it has to be in the message.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": "0-1416152/1416153"})
        raise AssertionError(f"the count is the refusal, so no page should be sent, but {request.method} was")

    small_settings = replace(settings, max_rows=1000, aggregate_scan_limit=10_000)
    request = QueryRequest(
        question="Total obligations in district 3",
        sources=[
            RelationSource(
                alias="s",
                dataset="usaspending",
                relation="all_entities",
                filters=[FilterSpec(column="congressional_district", operator=FilterOperator.EQ, value="03")],
            )
        ],
        aggregates=[AggregateSpec(function=AggregateFunction.SUM, column="total_obligation", alias="total")],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings,
            BdpRepository(small_settings, client),
            PostgrestTransport(small_settings, client),
        )
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(request)

    message = str(caught.value)
    assert "1416153 rows match" in message
    assert "1406153 more than the complete-scan limit of 10000" in message
    assert "usaspending.all_entities" in message
    # The count is only worth anything if it counts the rows this query asked for.
    assert requests[0].url.params["congressional_district"] == "eq.03"
    assert [request.method for request in requests] == ["HEAD"]


@pytest.mark.asyncio
async def test_a_join_over_the_row_limit_does_not_blame_the_scan_limit(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """Both sources here are scanned in full and are nowhere near the scan limit. The join is what
    produced too many rows, and the old message sent the caller to raise a limit that cannot fix it.
    """
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [{"uei": "A"}, {"uei": "A"}],
            "/ngopen/samer/sam_registrations": [{"uei": "A"}, {"uei": "A"}, {"uei": "A"}],
        },
    )
    small_settings = replace(settings, max_rows=4, aggregate_scan_limit=10)
    service = QueryService(
        small_settings,
        BdpRepository(small_settings, client),
        PostgrestTransport(small_settings, client),
    )
    try:
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(
                QueryRequest(
                    question="Match every recipient to every registration",
                    sources=[
                        RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
                        RelationSource(alias="sam", dataset="samer", relation="sam_registrations"),
                    ],
                    joins=[JoinSpec(left_alias="awards", right_alias="sam", left_column="uei", right_column="uei")],
                    allowed_reliability=[Reliability.RELIABLE],
                )
            )
    finally:
        await client.aclose()

    message = str(caught.value)
    assert "BENTHIC_MAX_ROWS" in message
    assert "BENTHIC_AGGREGATE_SCAN_LIMIT" not in message


@pytest.mark.asyncio
async def test_the_scan_refusal_does_not_promise_narrowing_that_cannot_work(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """A refusal that names an exit which does not exist costs the caller the turns it spends finding out.

    `usaspending.reporting_agency_overview` declares no primary key, so `fetch_complete` refuses an
    aggregate over it once the filter leaves more than one page of rows - a different rule from the
    complete-scan cap, and one that narrowing below the scan cap does not escape. Measured live:

        no filter (10,545 rows)   -> "...narrow the filters until each source matches at most 10000 rows"
        fiscal_year=2025 (1,221)   -> "...declares no primary key... narrow the filter so the result
                                     fits in one page of 1000 rows"

    So the first message advises exactly the action that produces the second. `query_having_text` took
    it, spent its remaining turns, and failed - in all 35 complete cycles in the corpus, before the
    relation was findable at all.

    For a source that declares a primary key the advice stands, because there narrowing does work. This
    asserts the difference rather than banning the sentence.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        return httpx.Response(200, headers={"Content-Range": "0-10544/10545"})

    small_settings = replace(settings, max_rows=1000, aggregate_scan_limit=10_000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings, BdpRepository(small_settings, client), PostgrestTransport(small_settings, client)
        )
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(
                build_single_query(
                    question="total obligation by agency",
                    source="usaspending.reporting_agency_overview",
                    select=None,
                    where=None,
                    group_by=["toptier_code"],
                    metrics=["total=sum:total_dollars_obligated_gtas"],
                    having=None,
                    order=None,
                    limit=10,
                    offset=0,
                )
            )
    message = str(caught.value)
    assert "narrow the filters until each source matches at most" not in message, (
        "for a relation with no primary key, narrowing below the scan cap cannot work - the aggregate is "
        f"refused for a different reason entirely. Message: {message}"
    )
    assert "primary key" in message, f"the refusal has to name the constraint it actually hit: {message}"


@pytest.mark.asyncio
async def test_the_no_primary_key_refusal_is_terminal_not_an_enumeration_invitation(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """A refusal that says "narrow to a single period" invites the caller to enumerate and then sum.

    `reporting_agency_overview` holds one row per agency per fiscal year and period, and has no primary
    key. Its aggregate is refused past one page, and narrowing below the scan cap does not escape that.
    The current refusal ends by telling the caller to narrow "until the result fits in one page, which
    here means naming a single period" - which is exactly the per-period enumeration that then cannot be
    summed into one total. Measured live: the model followed it into 5 turns and 31,062 chars of
    reasoning, listing 111 codes it could not combine, until the token budget cut it off.

    The refusal must be terminal: state that no sequence of queries reaches a cross-row total here, and
    that the answer is to decline, not to narrow.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        return httpx.Response(200, headers={"Content-Range": "0-10544/10545"})

    small_settings = replace(settings, max_rows=1000, aggregate_scan_limit=10_000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings, BdpRepository(small_settings, client), PostgrestTransport(small_settings, client)
        )
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(
                build_single_query(
                    question="total obligation by agency across all periods",
                    source="usaspending.reporting_agency_overview",
                    select=None,
                    where=None,
                    group_by=["toptier_code"],
                    metrics=["total=sum:total_dollars_obligated_gtas"],
                    having=None,
                    order=None,
                    limit=10,
                    offset=0,
                )
            )
    message = str(caught.value)
    assert "narrow until the result fits in one page" not in message, message
    assert "naming a single period" not in message, message
    assert "cannot be answered" in message, (
        f"the refusal must say the aggregate is unreachable and to decline, not to narrow: {message}"
    )


@pytest.mark.asyncio
async def test_the_scan_refusal_still_advises_narrowing_when_narrowing_works(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """The companion to the contract above, so the fix cannot be made by deleting the advice.

    `usaspending.all_entities` declares a primary key, so getting under the cap genuinely does let the
    aggregate proceed. Removing the exit from every refusal would trade one dead end for another.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        return httpx.Response(200, headers={"Content-Range": "0-10544/10545"})

    small_settings = replace(settings, max_rows=1000, aggregate_scan_limit=10_000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings, BdpRepository(small_settings, client), PostgrestTransport(small_settings, client)
        )
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(
                build_single_query(
                    question="count by state",
                    source="usaspending.all_entities",
                    select=None,
                    where=None,
                    group_by=["state"],
                    metrics=["n=count:*"],
                    having=None,
                    order=None,
                    limit=10,
                    offset=0,
                )
            )
    assert "narrow the filters" in str(caught.value).lower()


@pytest.mark.asyncio
async def test_rejects_multiple_unjoined_sources(settings: Any, bdp_documents: dict[str, Any]) -> None:
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [],
            "/ngopen/samer/sam_registrations": [],
        },
    )
    service = QueryService(
        settings,
        BdpRepository(settings, client),
        PostgrestTransport(settings, client),
    )

    with pytest.raises(QueryValidationError, match="require at least one signed join"):
        await service.execute(
            QueryRequest(
                question="Compare datasets",
                sources=[
                    RelationSource(alias="awards", dataset="usaspending", relation="all_entities"),
                    RelationSource(alias="sam", dataset="samer", relation="sam_registrations"),
                ],
            )
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_zero_rows_preserve_explicit_output_columns(settings: Any, bdp_documents: dict[str, Any]) -> None:
    client = _query_client(bdp_documents, {"/ngopen/usaspending/all_entities": []})
    service = QueryService(
        settings,
        BdpRepository(settings, client),
        PostgrestTransport(settings, client),
    )

    result = await service.execute(
        QueryRequest(
            question="Find one entity",
            sources=[
                RelationSource(
                    alias="awards",
                    dataset="usaspending",
                    relation="all_entities",
                    select=["uei"],
                    filters=[FilterSpec(column="uei", operator=FilterOperator.EQ, value="missing")],
                )
            ],
            output_columns=["awards.uei"],
        )
    )
    await client.aclose()

    assert result.rows == []
    assert result.columns == ["awards.uei"]


@pytest.mark.asyncio
async def test_ordering_a_column_of_mixed_types_is_a_total_order_not_a_crash(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """`order=` raised an uncaught TypeError on a column the manifest calls `string`.

    A declared `string` column is not guaranteed to hold strings: PostgREST returns whatever JSON
    type is stored, and a numeric-looking value comes back as a number. `_sort_value` tagged numbers
    and text with the same rank 1, so comparing `(1, 3)` with `(1, '7')` raised
    `'<' not supported between instances of 'str' and 'int'`. That escaped the tool wrapper, which
    catches BenthicMCPError and ValueError but not TypeError, so the caller got an internal error
    rather than a result. Found by contract-checking the sort key's totality, invisible to 670
    case-runs because no case orders by such a column.
    """
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [
                {"uei": "A", "name": 3},
                {"uei": "B", "name": "alpha"},
                {"uei": "C", "name": 1},
                {"uei": "D", "name": "beta"},
            ]
        },
    )
    service = QueryService(settings, BdpRepository(settings, client), PostgrestTransport(settings, client))
    request = QueryRequest(
        question="Order by name",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "name"])],
        order=[OutputOrder(column="s.name")],
    )

    try:
        result = unqualify_result(await service.execute(request))
    finally:
        await client.aclose()

    # Total order, deterministic, and every row present: sorting must not drop or invent rows.
    # Documented order: nulls, then numbers by magnitude, then text as lowercase.
    assert [row["uei"] for row in result.rows] == ["C", "A", "B", "D"]
    assert result.row_count == 4


@pytest.mark.asyncio
async def test_having_never_compares_text_with_a_number(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """`having` built a dict of all eight comparisons, so an equality also computed `>`.

    The dict literal is eager, so `having mx=eq.3` where `mx` is min() over a text column evaluated
    `value > expected` and raised `'>' not supported between instances of 'str' and 'int'`. The
    ordering operators are guarded by _numeric and refuse cleanly; equality and inequality were not,
    because nothing stopped the dict from computing the unguarded ones as well. TypeError is not a
    BenthicMCPError and not a ValueError, so it escaped the tool wrapper and the caller saw an
    internal error instead of a message naming the column. Same family as the sort-key crash, one
    layer over.
    """
    client = _query_client(
        bdp_documents,
        {
            "/ngopen/usaspending/all_entities": [
                {"uei": "A", "name": "alpha"},
                {"uei": "B", "name": "beta"},
            ]
        },
    )
    service = QueryService(settings, BdpRepository(settings, client), PostgrestTransport(settings, client))
    request = QueryRequest(
        question="Having an equality on a text aggregate",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "name"])],
        aggregates=[AggregateSpec(alias="mx", function=AggregateFunction.MIN, column="s.name")],
        having=[HavingSpec(column="mx", operator=FilterOperator.EQ, value=3)],
    )

    ordering = request.model_copy(update={"having": [HavingSpec(column="mx", operator=FilterOperator.GT, value=3)]})
    try:
        equality = unqualify_result(await service.execute(request))
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(ordering)
    finally:
        await client.aclose()

    assert equality.rows == [], "no text equals 3, so the answer is an empty result, not a crash"
    # The ordering operators still refuse, and the refusal names the column so the caller can act.
    assert "mx" in str(caught.value)


def _built(metrics: list[str] | None = None, having: list[str] | None = None) -> QueryRequest:
    return build_single_query(
        question="Total obligations by state",
        dataset="usaspending",
        relation="all_entities",
        select=None,
        where=None,
        group_by=["state"],
        metrics=metrics,
        having=having,
        order=None,
        limit=100,
        offset=0,
    )


def test_both_metric_spellings_build_the_same_aggregate() -> None:
    # The ':' spelling is accepted and was documented nowhere, so a caller who found it by trying
    # had no reason to prefer it and no reason to trust it.
    assert (
        _built(metrics=["total=sum:total_obligation"]).aggregates
        == _built(metrics=["total:sum:total_obligation"]).aggregates
    )


def test_a_bare_function_column_metric_is_rejected() -> None:
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["sum:total_obligation"])

    message = str(caught.value)
    assert "total_sum=sum:total_obligation" in message
    assert "The output name is missing" in message


@pytest.mark.parametrize("expression", ["total:sum:obligation:extra", "total=sum:obligation:extra"])
def test_a_column_containing_a_colon_is_rejected_where_it_is_typed(expression: str) -> None:
    """The capped split accepted a column of 'obligation:extra' and failed much later on it.

    The failure it reached instead was an unknown-column error from the fetched relation, so the
    caller was sent looking for a column that had never been asked for.
    """
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=[expression])

    message = str(caught.value)
    assert "total_sum=sum:total_obligation" in message
    # The reason names the colon: a name was given, so the missing-name reason would send the
    # caller to fix the part that is already right.
    assert "A column cannot contain ':'." in message


def test_an_empty_metric_column_is_a_named_error_and_not_a_pydantic_one() -> None:
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["total_sum=sum:"])

    assert "The source column is empty." in str(caught.value)


def test_whitespace_around_every_part_of_a_metric_is_tolerated() -> None:
    # Only the name was stripped, so 'n = sum : col' was reported as an unknown function ' sum '.
    assert (
        _built(metrics=[" total = sum : total_obligation "]).aggregates
        == _built(metrics=["total=sum:total_obligation"]).aggregates
    )


def test_an_empty_metric_name_is_a_named_error_and_not_a_pydantic_one() -> None:
    """An empty name reached AggregateSpec, so the caller read 'String should match pattern'.

    The tool wrapper catches ValueError, which a pydantic failure is, so the pattern text was
    served to the caller as the whole error: no form, no example, nothing to act on.
    """
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["=sum:total_obligation"])

    message = str(caught.value)
    assert type(caught.value) is QueryValidationError
    assert "total_sum=sum:total_obligation" in message
    assert "String should match" not in message


@pytest.mark.parametrize("expression", ["sum:total_obligation", "=sum:total_obligation", "total_sum"])
def test_the_metric_error_carries_an_example_that_parses(expression: str) -> None:
    """The point of the example is that a caller can copy it, so the test uses it.

    The message used to restate the form with no example, and the form said "name" where the tool
    docstring said "alias", so a caller read it as the source column and its correction, which was
    valid, was never confirmed.
    """
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=[expression])

    assert "total_sum=sum:total_obligation" in str(caught.value)
    assert _built(metrics=["total_sum=sum:total_obligation"]).aggregates[0].alias == "total_sum"


@pytest.mark.parametrize(
    "expression",
    ["total_sum", "total_sum:100000", ":gt:100000", ">100000"],
)
def test_a_malformed_having_names_both_accepted_forms(expression: str) -> None:
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["total_sum=sum:total_obligation"], having=[expression])

    message = str(caught.value)
    assert "total_sum>100000" in message
    assert "total_sum:gt:100000" in message
    assert "String should match" not in message


def test_an_unknown_having_operator_lists_the_accepted_ones() -> None:
    with pytest.raises(QueryValidationError) as caught:
        _built(metrics=["total_sum=sum:total_obligation"], having=["total_sum:above:100000"])

    message = str(caught.value)
    assert "above" in message
    # ne is not a name this parser has; neq is, and it is the one a caller reading the filter
    # operators would have written otherwise.
    for operator in ("eq", "neq", "gt", "gte", "lt", "lte"):
        assert operator in message


def test_the_having_example_the_message_gives_parses() -> None:
    assert _built(
        metrics=["total_sum=sum:total_obligation"],
        having=["total_sum>100000", "total_sum:gt:100000"],
    ).having == [
        HavingSpec(column="total_sum", operator=FilterOperator.GT, value=100000),
        HavingSpec(column="total_sum", operator=FilterOperator.GT, value=100000),
    ]


@pytest.mark.asyncio
async def test_a_bare_count_is_answered_from_the_count_head_rather_than_refused(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """A `count(*)` with no group_by produces one row whatever the source width, so the scan limit
    does not apply to it - but it was being refused anyway, and a model cannot narrow a count.

    Found by driving the live interface: `count(*)` on usaspending.reporting_agency_overview was
    refused at 10,545 rows against a 10,000 limit, and `count(*)` on usaspending.all_entities
    filtered to district 03 was refused at 1,416,153. There is no filter that makes either
    succeed, so both were dead ends, and the model burned its whole turn budget re-issuing them.

    PostgREST answers it without a scan. `select=count` is not gated by the server's
    db-aggregates-enabled setting (every `count()` and `col.sum()` spelling returns PGRST123), and
    the count HEAD this path already sends carries the exact total in Content-Range: verified
    `[{"count": 1416153}]` in 1.23s against a 17,884,243-row table. The scan is pure waste here.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-1416152/1416153"})
        # Any GET here would be the scan the count makes unnecessary.
        return httpx.Response(200, json=[])

    capped = replace(settings, max_rows=2, aggregate_scan_limit=3)
    request = QueryRequest(
        question="Count recipients in district 03",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities")],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column=None, alias="n")],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    assert result.rows == [{"n": 1416153}], f"expected the exact count, got {result.rows}"
    assert not [r for r in requests if r.method == "GET" and r.url.params.get("limit")], (
        "a bare count must not page the source"
    )


@pytest.mark.asyncio
async def test_a_bare_count_falls_back_to_the_scan_when_the_count_head_answers_nothing(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """The count HEAD is load-bearing for this shape now, so a silent failure must never produce a
    number. `count_matching` returns None on any failure by design; that has to mean "scan instead",
    not "answer zero"."""

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(405)  # count not supported here
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        # Bounded, so the scan terminates under the cap and the assertion is about which path
        # produced the number rather than about the guard.
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(offset, min(offset + limit, 5))])

    capped = replace(settings, max_rows=2, aggregate_scan_limit=10)
    request = QueryRequest(
        question="Count recipients",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities")],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column=None, alias="n")],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    assert result.rows == [{"n": 5}], f"must fall back to the scan and count it, got {result.rows}"


@pytest.mark.asyncio
async def test_a_grouped_count_still_uses_the_scan(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """The fast path is deliberately narrow. A grouped count has one output row per distinct group
    value, so its size depends on the data rather than on the source width, and the count HEAD
    cannot answer it - it would silently return the ungrouped total, which is a different number."""

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            # No count available, so neither the fast path nor the guard can fire and the only way
            # to answer is to scan and group. If the fast path were reachable for a grouped count it
            # would either refuse (a count of "everything" over the cap) or answer one row with the
            # ungrouped total; both fail the assertions below.
            return httpx.Response(200)
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        rows = [
            {"uei": str(index), "state": "MA" if index % 2 else "VT"} for index in range(offset, min(offset + limit, 8))
        ]
        return httpx.Response(200, json=rows)

    capped = replace(settings, max_rows=4, aggregate_scan_limit=10)
    request = QueryRequest(
        question="Count recipients by state",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "state"])],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column=None, alias="n")],
        group_by=["s.state"],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    assert len(result.rows) == 2, f"a grouped count must produce one row per group, got {result.rows}"
    assert {row["s.state"] for row in result.rows} == {"MA", "VT"}
    assert sorted(row["n"] for row in result.rows) == [4, 4], f"expected an even split of 8, got {result.rows}"


@pytest.mark.asyncio
async def test_the_bare_count_fast_path_is_not_reachable_when_the_count_would_overflow_the_guard(
    settings: Any,
    bdp_documents: dict[str, Any],
) -> None:
    """The fast path must be refused by shape, not merely be unreachable because of the cap.

    With a `group_by`, the count HEAD answers the number of matching *source* rows, which is not the
    number of output rows. Taking that path would answer `count(*)` over the whole filter and label
    it as a grouped result - a confidently wrong number rather than a refusal. The grouped contract
    above only passes today because the scan cap happens to stop it first, so it does not prove the
    shape is excluded. This one makes the cap irrelevant: the count is small enough to scan, and a
    fast path that had accepted the group_by would answer one row instead of one row per group.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-7/8"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        return httpx.Response(
            200,
            json=[
                {"uei": str(index), "state": "MA" if index % 2 else "VT"}
                for index in range(offset, min(offset + limit, 8))
            ],
        )

    capped = replace(settings, max_rows=10, aggregate_scan_limit=100)
    request = QueryRequest(
        question="Count recipients by state",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "state"])],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column=None, alias="n")],
        group_by=["s.state"],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    # A fast path that ignored the group_by would have returned [{"n": 8}] - one row, the ungrouped
    # total. Grouping 8 rows on a two-valued column has to give two rows.
    assert len(result.rows) == 2, f"the grouped shape was answered without grouping: {result.rows}"
    assert sorted(row["n"] for row in result.rows) == [4, 4]


@pytest.mark.asyncio
async def test_a_count_of_a_column_does_not_take_the_row_count(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """`count(column)` counts non-null values and `count(*)` counts rows. They are different
    numbers whenever the column has nulls, and the count HEAD only knows the row count.

    Letting `count(column)` onto the fast path answers the row count under the column's alias, which
    is a confidently wrong total rather than a refusal - and IRS organization tables are exactly the
    shape where `f990_total_assets_recent` is null for most rows. The scan is required.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-7/8"})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        # Four of eight rows have the column; the rest are null.
        rows = [
            {"uei": str(index), "duns": str(index) if index % 2 == 0 else None}
            for index in range(offset, min(offset + limit, 8))
        ]
        return httpx.Response(200, json=rows)

    capped = replace(settings, max_rows=10, aggregate_scan_limit=100)
    request = QueryRequest(
        question="How many entities have a DUNS recorded",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei", "duns"])],
        output_columns=["n"],
        aggregates=[AggregateSpec(function=AggregateFunction.COUNT, column="s.duns", alias="n")],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    assert result.rows == [{"n": 4}], f"count of a column must count non-null values, not rows; got {result.rows}"


def test_a_rejected_filter_names_the_operators_and_shows_one() -> None:
    """`column=value` is the first filter anyone writes, and the message used to say only
    "column=operator.value" without saying which operators exist.

    Four rejections in eighteen live probes were this shape - `congressional_district=ZZ`,
    `district_id=ZZ`, `congress_start=117` - and each one cost a turn for a spelling the model had
    no way to guess correctly from the message alone. The list is derived from FilterOperator so it
    cannot drift from the parser, and the null tests are listed separately because they are the two
    operators that take no value.
    """
    from benthic_mcp.models import FilterOperator
    from benthic_mcp.query import _parse_filter

    # The real path: `_parse_filter` supplies the reason, which is what makes the message specific.
    with pytest.raises(QueryValidationError) as caught:
        _parse_filter("congressional_district=ZZ")
    message = str(caught.value)

    for operator in FilterOperator:
        if operator in (FilterOperator.IS_NULL, FilterOperator.NOT_IS_NULL):
            continue
        assert operator.value in message, f"{operator.value} is accepted by the parser but absent from the message"
    assert "is.null" in message and "not.is.null" in message
    # A worked example, not just a template.
    assert "congressional_district=eq.03" in message
    # And the specific reason, so the model can see its own mistake rather than guess.
    assert "is a value, not an operator" in message


def test_every_filter_rejection_shares_one_message() -> None:
    """The two rejection sites drifted once already: the metric parser said `name=` while its own
    docstring said `alias=`. One builder means they cannot disagree again."""
    from benthic_mcp.query import _invalid_filter

    for expression in ("no_operator_here", "=novalue", "col=eq.a", "col=eq.a", "col=bogus.v"):
        assert "column=operator.value" in str(_invalid_filter(expression)), expression


def test_operator_and_function_names_are_case_insensitive() -> None:
    """A rejection that lists only lowercase spellings teaches a model the wrong thing.

    Found in a live transcript: the model wrote `alias=MAX:total_dollars_obligated_gtas`, was told
    `Unknown aggregate function 'MAX'; use count, sum, avg, min, max`, concluded in its reasoning
    "The aggregate function should be lowercase max", and then sent the uppercase spelling twice
    more - the same rejected call on consecutive turns. It had correctly identified the fix and
    re-sent the bug.

    A caller who reads a message and re-sends the spelling shown must not fail. Case is not
    information the server is protecting.
    """
    from benthic_mcp.models import AggregateFunction, FilterOperator
    from benthic_mcp.query import _parse_filter, _parse_metric

    for function in AggregateFunction:
        lower = _parse_metric(f"out={function.value}:col")
        upper = _parse_metric(f"out={function.value.upper()}:col")
        assert lower.function is upper.function, function

    for operator in FilterOperator:
        if operator.value in {"is.null", "not.is.null"}:
            # Compared whole, so case has to match on both halves at once.
            assert _parse_filter(f"c={operator.value.upper()}").operator is operator
            assert _parse_filter(f"c={operator.value.lower()}").operator is operator
            continue
        # `in` takes a JSON array; every other operator takes a scalar.
        value = '["a"]' if operator is FilterOperator.IN else "x"
        assert _parse_filter(f"c={operator.value.upper()}.{value}").operator is operator
        assert _parse_filter(f"c={operator.value.lower()}.{value}").operator is operator


def test_a_value_appended_to_a_null_test_names_the_whole_operator() -> None:
    """`is.null` and `not.is.null` are the only operators containing a dot, so `column=is.null.x`
    arrives at the unknown-operator branch already split in half. Reporting the fragment tells the
    caller nothing about what they wrote."""
    from benthic_mcp.query import _parse_filter

    for expression, expected in (
        ("col=is.null.x", "'is.null'"),
        ("col=not.is.null.y", "'not.is.null'"),
    ):
        with pytest.raises(QueryValidationError) as caught:
            _parse_filter(expression)
        message = str(caught.value)
        assert expected in message, f"{expression} -> {message}"
        assert "takes no value" in message


def test_a_filter_may_write_gt_and_lt_without_the_dot() -> None:
    """`col>0` is the first filter anyone writes after `col=value`, and the transcripts contain
    `total_dollars_obligated_gtas>0` and `total_obligation>0` from a model that had just been told
    the dot syntax and had not applied it.

    An embedded comparison operator is unambiguous - there is no reading of `col>0` other than
    greater-than zero - so it is accepted and mapped. That is a narrower change than accepting bare
    `col=value`, which this file deliberately keeps refusing: `is_current=true` could equally be
    meant as `eq.true` or be a typo for something else, and guessing turns a typo into a silently
    wrong result set instead of an error the model can read.
    """
    from benthic_mcp.query import _parse_filter

    assert _parse_filter("total_obligation>0") == FilterSpec(
        column="total_obligation", operator=FilterOperator.GT, value=0
    )
    assert _parse_filter("total_obligation>=100") == FilterSpec(
        column="total_obligation", operator=FilterOperator.GTE, value=100
    )
    assert _parse_filter("total_obligation<0") == FilterSpec(
        column="total_obligation", operator=FilterOperator.LT, value=0
    )
    assert _parse_filter("total_obligation<=0") == FilterSpec(
        column="total_obligation", operator=FilterOperator.LTE, value=0
    )
    assert _parse_filter("fiscal_year>=2020") == FilterSpec(
        column="fiscal_year", operator=FilterOperator.GTE, value=2020
    )


def test_a_bare_column_equals_value_is_still_refused() -> None:
    """The counterpart, and the reason the above is safe.

    `is_current=true` is what a model actually wrote. It could be meant as `eq.true`, or it could
    be `is_current=is.true` mistyped, or the value could belong after a different operator.
    Nothing in the expression distinguishes them, so it stays an error that names the problem
    rather than a guess that returns the wrong rows.
    """
    from benthic_mcp.query import _parse_filter

    with pytest.raises(QueryValidationError, match="is a value, not an operator"):
        _parse_filter("is_current=true")


@pytest.mark.asyncio
async def test_a_truncated_result_says_how_many_rows_exist_and_what_to_do(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """The defect that stopped a live session dead: `truncated: true` and nothing else.

    A session asked for entities in MA district 03, received 100 rows, and spent every remaining
    turn querying again. It could not know whether another call would help, because a truncated page
    of 100 out of 100 and a truncated page of 100 out of 1,416,153 look identical. The server knew
    the difference - it had just paid for a HEAD to find out - and used the number only to build
    refusal text, discarding it on the path that succeeded.

    The stop rule was already in the tool description on every turn and did not help, because a
    model cannot budget turns without knowing the size of what it is holding. The warning has to
    distinguish "you are holding everything" from "paging cannot produce an answer at this size".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-1416152/1416153"})
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(limit)])

    capped = replace(settings, max_rows=5, default_query_limit=5)
    request = QueryRequest(
        question="Entities in MA district 03",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities")],
        limit=5,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    assert result.truncated
    assert result.sources[0].matched_rows == 1416153
    text = " ".join(warning.message for warning in result.warnings)
    assert "1416153" in text, f"the true size must reach the caller: {text}"
    assert "answer now" in text.lower(), f"the warning must say what to do, not only what happened: {text}"


@pytest.mark.asyncio
async def test_a_complete_result_does_not_claim_to_be_truncated(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """The counterpart, and the case that matters most.

    A live probe returned 5 rows for a narrow filter and still reported `source_complete: false` and
    `truncated: true`, because the page came back full and the sentinel row proved a sixth existed.
    That is technically true and practically useless: the caller asked for 5 and got 5. If such a
    result also says "paging cannot produce an answer", a model is being told to give up on data it
    has not seen. So when the count shows the page holds everything, the warning says that instead.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-range": "0-4/5"})
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=[{"uei": str(index)} for index in range(limit)])

    capped = replace(settings, max_rows=5, default_query_limit=5)
    request = QueryRequest(
        question="Terms for district 3",
        sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities")],
        limit=5,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QueryService(capped, BdpRepository(capped, client), PostgrestTransport(capped, client)).execute(
            request
        )

    text = " ".join(warning.message for warning in result.warnings)
    assert "all 5 rows" in text, f"a complete result must not be described as lossy: {text}"
    assert "answer now" not in text.lower()


def test_a_filter_may_write_the_operator_separated_by_a_colon() -> None:
    """`column=eq:value` is the same filter as `column=eq.value`, and the transcripts show a model
    writing it repeatedly.

    From an unattended probe run: `district=eq:3`, `congressional_district=like:%MA-03%`. The
    refusal said "'eq:3' is a value, not an operator, so it has no operator before it" - which is
    wrong about the caller's intent. An operator *is* named, and only the separator differs.

    This is the same case as `column>0`, which is already accepted, and it is accepted for the same
    reason: there is one reading. `column=value` stays refused, because that names nothing.

    Recorded from an observer run rather than from the suite: no generated case writes this spelling,
    so the suite would never have found it.
    """
    from benthic_mcp.models import FilterOperator
    from benthic_mcp.query import _parse_filter

    assert _parse_filter("district=eq:3") == FilterSpec(column="district", operator=FilterOperator.EQ, value=3)
    assert _parse_filter("name=like:%MA%") == FilterSpec(column="name", operator=FilterOperator.LIKE, value="%MA%")
    # The dotted spelling still works, and both spellings agree.
    assert _parse_filter("district=eq.3") == _parse_filter("district=eq:3")


def test_an_unknown_colon_operator_is_still_refused() -> None:
    """Accepting the separator must not accept a made-up operator with it."""
    from benthic_mcp.query import _parse_filter

    with pytest.raises(QueryValidationError, match="'beside' is not an operator"):
        _parse_filter("district=beside:3")

    # The null tests take no value, so a colon after one is still wrong.
    with pytest.raises(QueryValidationError, match="takes no value"):
        _parse_filter("col=is.null:x")

    # `in` names itself unambiguously and its value is a JSON array either way, so it is accepted
    # with the colon too and parsed to the same list rather than refused.
    assert _parse_filter("ids=in:[1,2]").value == [1, 2]


async def _refusal_via_service(
    settings: Any,
    bdp_documents: dict[str, Any],
    aggregates: list[AggregateSpec],
    group_by: list[str] | None = None,
    matched: int = 50_284,
) -> str:
    """The message the model sees, reached the way the tool reaches it.

    Built through QueryService rather than by calling _scan_refusal directly, because a test of the
    private function passes while the served path does something else - which is exactly what
    happened here. Every direct call returned the right answer and the live server kept sending the
    pre-fix message, so the contract has to cross the same boundary the model does or it asserts
    nothing about what the model is told.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(request.url).split("?")[0])
        if document is not None:
            return httpx.Response(200, json=document)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": f"0-{matched - 1}/{matched}"})
        raise AssertionError(f"the refusal is the answer, so no page should be sent, but {request.method} was")

    small_settings = replace(settings, max_rows=1000, aggregate_scan_limit=10_000)
    query = QueryRequest(
        question="count them",
        sources=[
            RelationSource(
                alias="s",
                dataset="usaspending",
                relation="all_entities",
                filters=[FilterSpec(column="congressional_district", operator=FilterOperator.EQ, value="03")],
            )
        ],
        aggregates=aggregates,
        group_by=group_by or [],
    )
    # The tool layer qualifies aggregate columns with their source alias, so the request that reaches
    # QueryService carries `s.uei`, not `uei`. Testing the bare name left the branch inert on the
    # live server while this file passed: the lookup missed, the column read as unknown, and no claim
    # was made. Both spellings are asserted below so the alias cannot quietly stop being handled.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = QueryService(
            small_settings,
            BdpRepository(small_settings, client),
            PostgrestTransport(small_settings, client),
        )
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(query)
    return str(caught.value)


@pytest.mark.asyncio
async def test_a_refusal_over_cap_says_when_the_row_count_is_already_the_answer(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """A count-only query whose filters match 50284 rows was refused, and told to narrow.

    `truncation` asks for the count of organisations in Massachusetts and "I need the count to be
    reliable". The refusal names 50284 matching rows and then says narrow, so the model spent twelve
    turns enumerating subsection codes, foundation codes and affiliation codes to get a number it had
    already been given. Measured over 121 cap-then-success retries, the model narrowed 121 times and
    repeated the request zero times, so it is not failing to understand the instruction to narrow - it
    is being sent to narrow when narrowing cannot be what the question wants.

    When every aggregate is a count and nothing is grouped, the matching-row count is the answer, and
    the refusal should say so instead of spending the caller's turns.
    """
    message = await _refusal_via_service(
        settings, bdp_documents, [AggregateSpec(function=AggregateFunction.COUNT, column="uei", alias="n")]
    )

    assert "50284" in message
    assert "is the answer" in message, (
        "a count-only, ungrouped query was refused even though the matching-row count answers it; "
        "the message must say so rather than sending the caller off to narrow"
    )


@pytest.mark.asyncio
async def test_the_count_claim_survives_a_source_qualified_aggregate_column(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """The claim has to be reached through the spelling the tool actually sends.

    `benthic_query` qualifies an aggregate column with its source alias, so what arrives is
    `count:s.uei` against a definition holding bare `uei`. A lookup that does not strip the prefix
    finds no column, decides the column is unknown, and declines to say anything at all - which is
    exactly what happened: the three contracts above passed while the live server kept sending the
    pre-fix message to the model, for a full working day of observation cycles.
    """
    message = await _refusal_via_service(
        settings,
        bdp_documents,
        [AggregateSpec(function=AggregateFunction.COUNT, column="s.uei", alias="n")],
    )

    assert "is the answer" in message, (
        "a source-qualified count column is what the tool sends; the refusal must still recognise it"
    )


@pytest.mark.asyncio
async def test_a_refusal_does_not_claim_the_row_count_is_a_nullable_columns_count(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """The claim above is only sound for count(*) and for columns the manifest says cannot be null.

    The model asked for `count(ein)` and `ein` is nullable, so 50284 matching rows is an upper bound on
    the answer and not the answer. Reporting it as the answer would be worse than the current refusal,
    because a wrong number reads as a result.
    """
    message = await _refusal_via_service(
        settings, bdp_documents, [AggregateSpec(function=AggregateFunction.COUNT, column="name", alias="n")]
    )

    assert "is the answer" not in message
    assert "not.is.null" in message, (
        "the caller needs the one narrowing that makes the two numbers equal, which is excluding the "
        "nulls from the counted column"
    )


@pytest.mark.asyncio
async def test_a_refusal_makes_no_claim_when_grouping_or_a_non_count_is_asked_for(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """Row count is not the answer to a per-group count, a sum, or a total over several sources."""
    grouped = await _refusal_via_service(
        settings,
        bdp_documents,
        [AggregateSpec(function=AggregateFunction.COUNT, column="uei", alias="n")],
        group_by=["state"],
    )
    summed = await _refusal_via_service(
        settings, bdp_documents, [AggregateSpec(function=AggregateFunction.SUM, column="ein", alias="t")]
    )

    assert "is the answer" not in grouped, "a grouped count is not answered by the total row count"
    assert "is the answer" not in summed, "a sum is not a row count"
    # And the message the model needs either way still stands.
    for message in (grouped, summed):
        assert "50284" in message
        assert "narrow the filters" in message.lower()
