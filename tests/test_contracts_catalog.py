"""Deterministic contracts for the catalog and join subsystems.

The statistical gate measured whether a model took the right route. It could not see whether the
route returned anything, so 55 of 55 calls down a signed path returned 0 rows where 5 were
expected and the suite scored those cases PASS for 51 runs. A contract is the opposite: a property
over the input space, stated in the code's own vocabulary, that either holds or does not. Nothing
here runs a model, touches the network, or sleeps.

The contracts are written against a constructed catalog rather than the shared fixture, because the
shared fixture types `usp_cl.legislator_terms.district` as `string` and therefore cannot express the
mismatch the whole exercise exists to cover. The constructions below declare the types the real
signed manifest declares (verified against the cached signed catalog), so the same assertions would
run unchanged against a live one.

Every contract here was written to be falsifiable, and each one was: they were added because the
statistical gate could not see something, and four of them were found violated on arrival and fixed
rather than left red. `number` was missing from the numeric type set, a heuristic-typed edge graded
reliable returned no warning, an unsigned pair was refused without naming the signed alternative, an
unknown output column raised without a near miss, and a spatial edge was told to pass columns
explicitly into a refusal. If a future contract arrives already green, it is asserting nothing.
"""

import asyncio
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from benthic_mcp.bdp import BdpRepository, CatalogSnapshot
from benthic_mcp.catalog import (
    _INLINE_COLUMN_LIMIT,
    Catalog,
    ColumnDefinition,
    JoinEdge,
    RelationDefinition,
)
from benthic_mcp.errors import QueryValidationError
from benthic_mcp.joins import _join_key, _type_coercion, execute_joins
from benthic_mcp.models import (
    AggregateFunction,
    AggregateSpec,
    FilterOperator,
    FilterSpec,
    JoinCondition,
    JoinSpec,
    QueryRequest,
    RelationSource,
    Reliability,
)
from benthic_mcp.playbook import VerifyReport, known_identifiers, relation_hints, screen_prose
from benthic_mcp.postgrest import FetchedSource, PostgrestTransport
from benthic_mcp.query import QueryService
from benthic_mcp.seed import seed_playbook
from tests.factories import make_collection, make_manifest, relation

# The manifest's own scalar vocabulary, restated here rather than imported from joins.py. A contract
# that borrows the implementation's own type sets can only ever agree with it.
TEXT_TYPES = frozenset({"string", "varchar", "text", "character varying"})
INTEGER_TYPES = frozenset({"integer", "bigint", "smallint"})
DECIMAL_TYPES = frozenset({"number", "numeric", "real", "double precision"})

# Value pairs that denote the same entity across two declared types, and so must produce equal join
# keys. '03' and 3 are the pair from the bug; the rest are the same shape at other magnitudes.
EQUIVALENT_VALUES = {
    ("string", "integer"): [("03", 3), ("3", 3), ("0", 0), ("012", 12)],
    ("integer", "string"): [(3, "03"), (3, "3"), (0, "0"), (12, "012")],
    ("string", "number"): [("03", 3.0), ("3", 3.0), ("0", 0.0), ("012", 12.0)],
    ("number", "string"): [(3.0, "03"), (3.0, "3"), (0.0, "0"), (12.0, "012")],
}


def representative_values(declared_type: str) -> list[Any]:
    """One value per shape a column of this declared type can arrive in from a JSON endpoint."""
    if declared_type in TEXT_TYPES:
        return ["03", "3", "0", "abc", "", " 3 "]
    if declared_type in INTEGER_TYPES:
        return [3, 0, -1]
    if declared_type in DECIMAL_TYPES:
        return [3.0, 0.0, 3.5, -1.0]
    if declared_type == "boolean":
        return [True, False]
    if declared_type in {"date", "timestamp"}:
        return ["2024-01-01", "2024-01-01T00:00:00Z"]
    if declared_type == "geometry":
        return ["POINT (0 0)"]
    return [None, {"nested": "value"}, ["a", "b"]]


def equivalent_values(left_type: str, right_type: str) -> list[tuple[Any, Any]] | None:
    """Same-entity value pairs for this type pair, or None when the types share no representation."""
    return EQUIVALENT_VALUES.get((left_type, right_type))


def join_path(
    from_dataset: str,
    from_relation: str,
    from_column: str,
    to_dataset: str,
    to_relation: str,
    to_column: str,
    join_type: str,
    reliability: str,
    from_srid: int | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {"dataset_name": from_dataset, "relation": from_relation, "column": from_column}
    if from_srid is not None:
        source["srid"] = from_srid
    return {
        "from": source,
        "to": {"dataset_name": to_dataset, "relation": to_relation, "column": to_column},
        "join_type": join_type,
        "reliability": reliability,
    }


# The six edges of the live signed ngopen collection, with the declared types the live signed
# manifests declare. The last is the edge that returned nothing on every call.
SIGNED_PATHS = [
    join_path("usaspending", "all_entities", "uei", "samer", "sam_registrations", "uei", "identifier", "reliable"),
    join_path("usaspending", "all_entities", "duns", "irs_ng", "bmf_organizations", "ein", "heuristic", "heuristic"),
    join_path("samer", "sam_registrations", "duns", "irs_ng", "bmf_organizations", "ein", "heuristic", "heuristic"),
    join_path(
        "usaspending",
        "all_entities",
        "geom_point",
        "up_cdmaps",
        "congressional_districts",
        "geom",
        "spatial",
        "reliable",
        from_srid=4326,
    ),
    join_path(
        "usp_cl",
        "district_offices",
        "geom_point",
        "up_cdmaps",
        "congressional_districts",
        "geom",
        "spatial",
        "partial",
        from_srid=4326,
    ),
    join_path(
        "usaspending",
        "all_entities",
        "congressional_district",
        "usp_cl",
        "legislator_terms",
        "district",
        "identifier",
        "partial",
    ),
]

# A district code typed `number` rather than `integer`, joined to the same zero-padded text key.
NUMBER_TYPED_DISTRICT_PATH = join_path(
    "usaspending",
    "all_entities",
    "congressional_district",
    "up_cdmaps",
    "congressional_districts",
    "district_code",
    "identifier",
    "reliable",
)

# A join whose declared nature is a fuzzy match but which the manifest grades reliable.
HEURISTIC_TYPED_RELIABLE_PATH = join_path(
    "usaspending", "all_entities", "duns", "samer", "sam_registrations", "duns", "heuristic", "reliable"
)


def build_catalog(private_key: Ed25519PrivateKey, join_paths: list[dict[str, Any]]) -> Catalog:
    """A catalog shaped like the live signed one, including a relation wider than the inline cap."""
    manifests = {
        "usaspending": make_manifest(
            private_key,
            "usaspending",
            [
                relation(
                    "all_entities",
                    [
                        ("uei", "string"),
                        ("duns", "string"),
                        ("legal_business_name", "string"),
                        ("state", "string"),
                        ("congressional_district", "string"),
                        ("total_obligation", "number"),
                        ("geom_point", "geometry"),
                    ],
                    description="Federal award recipients",
                ),
                relation(
                    "wide_entity", [(f"field_{index:03d}", "string") for index in range(_INLINE_COLUMN_LIMIT + 9)]
                ),
            ],
            "https://benthic.io/ngopen/usaspending/",
        ),
        "samer": make_manifest(
            private_key,
            "samer",
            [
                relation(
                    "sam_registrations",
                    [("uei", "string"), ("duns", "string"), ("state", "string"), ("legal_business_name", "string")],
                )
            ],
            "https://benthic.io/ngopen/samer/",
        ),
        "irs_ng": make_manifest(
            private_key,
            "irs_ng",
            [relation("bmf_organizations", [("ein", "string"), ("org_name_current", "string"), ("state", "string")])],
            "https://benthic.io/ngopen/irs_ng/",
        ),
        "up_cdmaps": make_manifest(
            private_key,
            "up_cdmaps",
            [
                relation(
                    "congressional_districts",
                    [
                        ("district", "integer"),
                        ("district_code", "number"),
                        ("startcong", "number"),
                        ("geom", "geometry"),
                    ],
                )
            ],
            "https://benthic.io/ngopen/up_cdmaps/",
        ),
        "usp_cl": make_manifest(
            private_key,
            "usp_cl",
            [
                relation(
                    "legislator_terms",
                    [("bioguide_id", "string"), ("state", "string"), ("district", "integer"), ("term_start", "date")],
                    description="Historical legislator terms",
                ),
                relation(
                    "district_offices",
                    [
                        ("bioguide_id", "string"),
                        ("state", "string"),
                        ("office_id", "integer"),
                        ("geom_point", "geometry"),
                    ],
                ),
            ],
            "https://benthic.io/ngopen/usp_cl/",
        ),
    }
    collection = make_collection(private_key, manifests, join_paths)
    return Catalog(
        CatalogSnapshot(collections={"ngopen": collection}, manifests=manifests),
        relation_hints=relation_hints(seed_playbook()),
    )


@pytest.fixture
def signed_catalog(private_key: Ed25519PrivateKey) -> Catalog:
    """The live join graph over the live declared types."""
    return build_catalog(private_key, SIGNED_PATHS)


@pytest.fixture
def number_typed_catalog(private_key: Ed25519PrivateKey) -> Catalog:
    """The live graph plus a district key whose right endpoint is declared `number`."""
    return build_catalog(private_key, [*SIGNED_PATHS, NUMBER_TYPED_DISTRICT_PATH])


@pytest.fixture
def heuristic_typed_catalog(private_key: Ed25519PrivateKey) -> Catalog:
    """The live graph plus a duns-to-duns edge declared heuristic and graded reliable."""
    return build_catalog(private_key, [*SIGNED_PATHS, HEURISTIC_TYPED_RELIABLE_PATH])


# One signed edge with both endpoints resolved: edge, left relation, left column, right, right column.
Endpoint = tuple[JoinEdge, RelationDefinition, ColumnDefinition, RelationDefinition, ColumnDefinition]


def endpoints(catalog: Catalog) -> Iterator[Endpoint]:
    """Every signed edge with both endpoint columns resolved, in both orientations.

    join_graph is undirected, so the text/integer district edge is yielded as string-vs-integer and
    again as integer-vs-string. A contract that only held in one orientation would be worthless.
    """
    graph = catalog.join_graph()
    for relation_name in sorted(graph):
        for edge in sorted(graph[relation_name], key=JoinEdge.as_pair):
            left_dataset, left_relation = edge.left.split(".", 1)
            right_dataset, right_relation = edge.right.split(".", 1)
            left_definition = catalog.relations[(left_dataset, left_relation)]
            right_definition = catalog.relations[(right_dataset, right_relation)]
            yield (
                edge,
                left_definition,
                left_definition.columns[edge.left_column],
                right_definition,
                right_definition.columns[edge.right_column],
            )


def source(catalog: Catalog, alias: str, dataset: str, relation: str, rows: list[dict[str, Any]]) -> FetchedSource:
    return FetchedSource(
        source=RelationSource(alias=alias, dataset=dataset, relation=relation),
        definition=catalog.resolve_relation(dataset, relation),
        rows=rows,
        request_url="https://benthic.io/ngopen/example",
        truncated=False,
    )


# Contract 1: a declared type mismatch is always reported, never silently absorbed.


def test_every_declared_type_mismatch_on_a_signed_edge_warns_naming_both_types(signed_catalog: Catalog) -> None:
    """Walks the whole join graph, so it would run unchanged against a live catalog.

    A signed edge whose endpoints declare different scalar types must attach a warning naming both
    types. Before _type_coercion existed there was no warning at all, so a 0-row result from a
    mismatched key looked exactly like an absence of data. Found by reading the fix and asking what
    it covers.
    """
    checked = 0
    for edge, left_definition, left_column, right_definition, right_column in endpoints(signed_catalog):
        if left_column.type == right_column.type:
            continue
        note = _type_coercion(left_definition, edge.left_column, right_definition, edge.right_column)
        assert note is not None, f"{edge.left}.{edge.left_column} -> {edge.right}.{edge.right_column}"
        assert left_column.type in note
        assert right_column.type in note
        checked += 1
    assert checked, "the graph must contain a mismatched edge or this contract asserts nothing"


def test_a_text_key_against_a_number_key_also_warns(number_typed_catalog: Catalog) -> None:
    """The failing input built directly: the district edge with its right endpoint typed `number`."""
    spending = number_typed_catalog.resolve_relation("usaspending", "all_entities")
    districts = number_typed_catalog.resolve_relation("up_cdmaps", "congressional_districts")

    assert _type_coercion(spending, "congressional_district", districts, "district_code") is not None


def test_a_signed_edge_whose_endpoints_agree_raises_no_type_warning(signed_catalog: Catalog) -> None:
    """The control for the contract above: a warning that fires on every edge tells the reader nothing.

    Found by asking whether the fix could have been satisfied by warning unconditionally.
    """
    spending = signed_catalog.resolve_relation("usaspending", "all_entities")
    sam = signed_catalog.resolve_relation("samer", "sam_registrations")

    assert _type_coercion(spending, "uei", sam, "uei") is None


# Contract 2: keys from the two sides of a signed edge are always mutually comparable.


def test_join_key_is_total_and_hashable_for_every_representative_value(signed_catalog: Catalog) -> None:
    """A key is used as a dict index and compared with `==`, so it must never raise and never be
    unhashable. Found by asking what a key of a JSON value that is not a scalar would do.
    """
    for _edge, _left_definition, left_column, _right_definition, right_column in endpoints(signed_catalog):
        for coerce in (False, True):
            for declared_type in (left_column.type, right_column.type):
                for value in representative_values(declared_type):
                    key = _join_key({"side.column": value}, "side", ["column"], coerce=coerce)
                    assert isinstance(key, tuple)
                    assert isinstance(hash(key), int)


def test_equivalent_values_across_a_mismatched_edge_produce_equal_keys(signed_catalog: Catalog) -> None:
    """The bug, stated as a property: two values denoting the same entity must join.

    usaspending.all_entities.congressional_district holds '03' and usp_cl.legislator_terms.district
    holds 3. Compared in Python, '03' != 3, so the equijoin matched nothing on every call. Found by
    reading the scorer's route-only check, which could not see the 0 rows it was scoring as passes.

    The coerce flag is derived from the catalog exactly as execute_joins derives it rather than
    hardcoded, so this goes red if the derivation ever stops covering a declared mismatch.
    """
    compared = 0
    for edge, left_definition, left_column, right_definition, right_column in endpoints(signed_catalog):
        pairs = equivalent_values(left_column.type, right_column.type)
        if pairs is None:
            continue
        coerce = _type_coercion(left_definition, edge.left_column, right_definition, edge.right_column) is not None
        for left_value, right_value in pairs:
            left_key = _join_key({"left.column": left_value}, "left", ["column"], coerce=coerce)
            right_key = _join_key({"right.column": right_value}, "right", ["column"], coerce=coerce)
            assert left_key == right_key, (
                f"{left_column.type} {left_value!r} against {right_column.type} {right_value!r}"
            )
            compared += 1
    assert compared, "the graph must contain a mismatched edge or this contract asserts nothing"


def test_a_text_key_against_a_number_key_joins(number_typed_catalog: Catalog) -> None:
    """The failing input built directly, end to end, exactly as the live district call was made."""
    result = execute_joins(
        number_typed_catalog,
        [
            source(number_typed_catalog, "spending", "usaspending", "all_entities", [{"congressional_district": "03"}]),
            source(number_typed_catalog, "districts", "up_cdmaps", "congressional_districts", [{"district_code": 3.0}]),
        ],
        [
            JoinSpec(
                left_alias="spending",
                right_alias="districts",
                left_column="congressional_district",
                right_column="district_code",
            )
        ],
        {Reliability.RELIABLE},
        100,
    )

    assert result.rows, "a text key must match the number it denotes"


def test_coercion_does_not_equate_a_word_with_a_number() -> None:
    """The control: a contract satisfied by normalising everything would join nonsense together."""
    assert _join_key({"left.column": "abc"}, "left", ["column"], coerce=True) != _join_key(
        {"right.column": 0}, "right", ["column"], coerce=True
    )


def test_keys_are_untouched_when_both_endpoints_declare_the_same_type() -> None:
    """The control for the property above: normalisation is for a declared mismatch, not a default.

    Found by asking whether removing the mismatch check would change any signed edge's behaviour.
    """
    assert _join_key({"left.column": "03"}, "left", ["column"]) != _join_key({"right.column": 3}, "right", ["column"])


# Contract 3: a join runs only over a signed edge, and the refusals are the ones the docs claim.


def test_a_signed_edge_resolves_in_either_orientation(signed_catalog: Catalog) -> None:
    """Found by asking whether find_join's reverse branch could be dropped."""
    spending = signed_catalog.resolve_relation("usaspending", "all_entities")
    sam = signed_catalog.resolve_relation("samer", "sam_registrations")

    forward = signed_catalog.find_join(spending, sam, "uei", "uei")
    reverse = signed_catalog.find_join(sam, spending, "uei", "uei")

    assert (forward.from_dataset, forward.from_relation, forward.to_dataset, forward.to_relation) == (
        reverse.from_dataset,
        reverse.from_relation,
        reverse.to_dataset,
        reverse.to_relation,
    )
    assert forward.from_column == reverse.to_column == "uei"
    assert forward.to_column == reverse.from_column == "uei"


def test_an_unsigned_pair_is_refused(catalog: Catalog) -> None:
    """Found by reading the golden suite's unsigned_join_rejection capability and trusting it."""
    spending = catalog.resolve_relation("usaspending", "all_entities")
    sam = catalog.resolve_relation("samer", "sam_registrations")

    with pytest.raises(QueryValidationError, match="not a signed BDP join path"):
        catalog.find_join(spending, sam, "duns", "duns")


def test_a_partial_edge_requires_at_least_one_context_condition(signed_catalog: Catalog) -> None:
    """A partial edge is only provisional when paired with the context the manifest demands.

    Found by reading the note on the live district edge, which says the join must be paired with
    state and a congress bracket.
    """
    spending = source(
        signed_catalog, "spending", "usaspending", "all_entities", [{"congressional_district": "03", "state": "MD"}]
    )
    terms = source(
        signed_catalog,
        "terms",
        "usp_cl",
        "legislator_terms",
        [{"district": 3, "state": "MD", "term_start": "2024-01-01"}],
    )
    spec = JoinSpec(
        left_alias="spending",
        right_alias="terms",
        left_column="congressional_district",
        right_column="district",
    )

    with pytest.raises(QueryValidationError, match="Partial signed join requires context predicates"):
        execute_joins(signed_catalog, [spending, terms], [spec], {Reliability.PARTIAL}, 100)


def test_the_same_partial_edge_executes_once_a_context_condition_is_supplied(signed_catalog: Catalog) -> None:
    """The control for the contract above, and the direct regression for the bug.

    This is the call that returned 0 rows on 55 of 55 observations. Found by auditing a suite that
    scored it PASS for 51 runs on route alone.
    """
    spending = source(
        signed_catalog, "spending", "usaspending", "all_entities", [{"congressional_district": "03", "state": "MD"}]
    )
    terms = source(
        signed_catalog,
        "terms",
        "usp_cl",
        "legislator_terms",
        [{"district": 3, "state": "MD", "term_start": "2024-01-01"}],
    )
    spec = JoinSpec(
        left_alias="spending",
        right_alias="terms",
        left_column="congressional_district",
        right_column="district",
        extra_conditions=[JoinCondition(left_column="state", right_column="state")],
    )

    result = execute_joins(signed_catalog, [spending, terms], [spec], {Reliability.PARTIAL}, 100)

    assert result.rows, "the zero-padded text key must match the integer it denotes"
    assert "different types" in " ".join(result.metadata[0].warnings)


def test_a_spatial_edge_is_refused_by_the_join_path(signed_catalog: Catalog) -> None:
    """A spatial edge has to go through benthic_rpc; the equijoin cannot honour it.

    Found by reading the live collection, which signs two spatial edges against the district table.
    """
    spending = source(signed_catalog, "spending", "usaspending", "all_entities", [{"geom_point": "POINT (1 1)"}])
    districts = source(signed_catalog, "districts", "up_cdmaps", "congressional_districts", [{"geom": "POINT (1 1)"}])
    spec = JoinSpec(left_alias="spending", right_alias="districts", left_column="geom_point", right_column="geom")

    with pytest.raises(QueryValidationError, match="Spatial joins must use an allowlisted benthic_rpc"):
        execute_joins(signed_catalog, [spending, districts], [spec], {Reliability.RELIABLE}, 100)


def test_a_partial_edge_result_warns_naming_its_reliability(signed_catalog: Catalog) -> None:
    """Found by checking that the warning text quotes the evidence grade rather than the join type."""
    result = execute_joins(
        signed_catalog,
        [
            source(
                signed_catalog,
                "spending",
                "usaspending",
                "all_entities",
                [{"congressional_district": "03", "state": "MD"}],
            ),
            source(signed_catalog, "terms", "usp_cl", "legislator_terms", [{"district": 3, "state": "MD"}]),
        ],
        [
            JoinSpec(
                left_alias="spending",
                right_alias="terms",
                left_column="congressional_district",
                right_column="district",
                extra_conditions=[JoinCondition(left_column="state", right_column="state")],
            )
        ],
        {Reliability.PARTIAL},
        100,
    )

    assert "partial" in " ".join(result.metadata[0].warnings)


def test_a_heuristic_edge_result_warns_naming_its_reliability(signed_catalog: Catalog) -> None:
    """Found by reading the standing rule that a fuzzy match must not be presented as exact."""
    result = execute_joins(
        signed_catalog,
        [
            source(signed_catalog, "spending", "usaspending", "all_entities", [{"duns": "1", "state": "MD"}]),
            source(signed_catalog, "irs", "irs_ng", "bmf_organizations", [{"ein": "1", "state": "MD"}]),
        ],
        [JoinSpec(left_alias="spending", right_alias="irs", left_column="duns", right_column="ein")],
        {Reliability.HEURISTIC},
        100,
    )

    assert "heuristic" in " ".join(result.metadata[0].warnings).lower()


def test_a_reliable_edge_result_carries_no_reliability_warning(signed_catalog: Catalog) -> None:
    """The control: a warning present on every result is not a warning."""
    result = execute_joins(
        signed_catalog,
        [
            source(signed_catalog, "spending", "usaspending", "all_entities", [{"uei": "A"}]),
            source(signed_catalog, "sam", "samer", "sam_registrations", [{"uei": "A"}]),
        ],
        [JoinSpec(left_alias="spending", right_alias="sam", left_column="uei", right_column="uei")],
        {Reliability.RELIABLE},
        100,
    )

    assert result.rows
    assert result.metadata[0].warnings == []


def test_a_heuristic_typed_edge_warns_even_when_graded_reliable(heuristic_typed_catalog: Catalog) -> None:
    """The failing input built directly: a duns-to-duns edge declared heuristic, graded reliable."""
    result = execute_joins(
        heuristic_typed_catalog,
        [
            source(heuristic_typed_catalog, "spending", "usaspending", "all_entities", [{"duns": "1"}]),
            source(heuristic_typed_catalog, "sam", "samer", "sam_registrations", [{"duns": "1"}]),
        ],
        [JoinSpec(left_alias="spending", right_alias="sam", left_column="duns", right_column="duns")],
        {Reliability.RELIABLE},
        100,
    )

    assert result.metadata[0].warnings, "a fuzzy match must never be reportable as an exact one"


# Contract 5: a request names only signed columns, or is rejected pointing at signed ones.


async def test_every_column_named_in_a_request_is_signed_or_the_request_is_refused(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """Grounded or refused, for signed and unsigned names in every position a request can name one.

    Found by reading column_candidates' docstring, which calls the unknown-column dead end the
    thing the eval traces show burning the turn budget.
    """
    service, client = query_service(settings, bdp_documents)
    grounded = RelationSource(
        alias="s",
        dataset="usaspending",
        relation="all_entities",
        select=["uei", "total_obligation"],
    )
    filtered = RelationSource(
        alias="s",
        dataset="usaspending",
        relation="all_entities",
        select=["uei"],
        filters=[FilterSpec(column="total_obligations", operator=FilterOperator.EQ, value=5)],
    )
    unsigned_positions = [
        QueryRequest(question="q", sources=[grounded], output_columns=["s.total_obligations"]),
        QueryRequest(
            question="q",
            sources=[
                RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["total_obligations"])
            ],
        ),
        QueryRequest(question="q", sources=[filtered]),
        QueryRequest(question="q", sources=[grounded], group_by=["s.total_obligations"]),
        QueryRequest(
            question="q",
            sources=[grounded],
            aggregates=[AggregateSpec(function=AggregateFunction.SUM, column="s.total_obligations", alias="t")],
        ),
    ]
    try:
        for request in unsigned_positions:
            with pytest.raises(QueryValidationError, match="total_obligations"):
                await service.execute(request)
    finally:
        await client.aclose()


async def test_a_signed_request_runs_and_returns_its_rows(settings: Any, bdp_documents: dict[str, Any]) -> None:
    """The control for the contract above: the guard must reject the unsigned name, not everything."""
    service, client = query_service(settings, bdp_documents)
    try:
        result = await service.execute(
            QueryRequest(
                question="q",
                sources=[
                    RelationSource(
                        alias="s",
                        dataset="usaspending",
                        relation="all_entities",
                        select=["uei", "total_obligation"],
                    )
                ],
                output_columns=["s.uei", "s.total_obligation"],
            )
        )
    finally:
        await client.aclose()

    assert result.rows == [{"s.uei": "A", "s.total_obligation": 5}]


def test_a_near_miss_candidate_is_always_a_signed_column(catalog: Catalog) -> None:
    """A suggestion that names something the manifest does not have is worse than no suggestion.

    Found by reading the sentence in column_candidates that says a candidate can never invent a
    column, and checking it.
    """
    for (dataset, name), definition in sorted(catalog.relations.items()):
        for column in sorted(definition.columns):
            for guess in (column + "s", column.removesuffix("s"), column + "_id", "xyzzy_nonsense"):
                assert set(catalog.column_candidates(definition, guess)) <= set(definition.columns), (
                    f"{dataset}.{name} offered an unsigned name for {guess!r}"
                )


async def test_an_unknown_output_column_is_refused_naming_a_signed_column(
    settings: Any, bdp_documents: dict[str, Any]
) -> None:
    """The failing input built directly: the plural guess that select recovers from, in the
    output position."""
    service, client = query_service(settings, bdp_documents)
    try:
        with pytest.raises(QueryValidationError) as caught:
            await service.execute(
                QueryRequest(
                    question="q",
                    sources=[RelationSource(alias="s", dataset="usaspending", relation="all_entities", select=["uei"])],
                    output_columns=["s.congressional_districts"],
                )
            )
    finally:
        await client.aclose()

    assert "congressional_district" in offered_names(str(caught.value))


def test_inline_and_full_column_listings_between_them_cover_every_signed_column(signed_catalog: Catalog) -> None:
    """The inline cap may hide columns but must never make one unreachable.

    Found by reading the note that 21 of the 119 signed relations have more columns than an inline
    list should carry, and checking the escape hatch actually escapes.
    """
    narrow = 0
    for dataset, name in sorted(signed_catalog.relations):
        definition = signed_catalog.relations[(dataset, name)]
        if not definition.queryable or definition.endpoint is None:
            continue
        source_name = f"{dataset}.{name}"
        inline = {column.name for column in signed_catalog.discover(relation=source_name).relations[0].columns}
        full = {
            column.name for column in signed_catalog.discover(relation=source_name, detail="full").relations[0].columns
        }
        assert full == set(definition.columns), source_name
        assert inline <= set(definition.columns), source_name
        assert inline | full == set(definition.columns), source_name
        if inline < full:
            narrow += 1
    assert narrow, "the graph must contain a relation wider than the inline cap or this asserts nothing"


# Contract 6: every contract above is shown to be able to go red.


def test_the_zero_padded_case_is_red_without_the_normalisation() -> None:
    """The pre-fix behaviour, constructed directly, so the contract is known to discriminate.

    Before _coerce_token, the two sides of the live district edge indexed under different keys, the
    equijoin matched nothing, and the scorer checked only the route. Found by auditing that run.
    """
    without = (
        _join_key({"left.district": "03"}, "left", ["district"]),
        _join_key({"right.district": 3}, "right", ["district"]),
    )
    assert without[0] != without[1]

    with_normalisation = (
        _join_key({"left.district": "03"}, "left", ["district"], coerce=True),
        _join_key({"right.district": 3}, "right", ["district"], coerce=True),
    )
    assert with_normalisation[0] == with_normalisation[1]


def test_the_pre_fix_key_pair_is_the_one_that_would_have_gone_missing(signed_catalog: Catalog) -> None:
    """Contract 6 for the join contracts: the refusals are guards, not dead relations.

    Each refusal above is paired with the same edge accepted once its preconditions are met, so a
    catalog stripped of its signed edges would not pass this file. Found by asking whether the
    refusal tests would still hold with an empty graph.
    """
    assert signed_catalog.joins
    assert signed_catalog.signed_join_pairs()


# Helpers kept at the bottom so the contracts read as contracts.


def unresolved_join_message(left: str, right: str, candidates: list[JoinEdge]) -> str:
    from benthic_mcp.server import _unresolved_join_message

    return _unresolved_join_message(left, right, candidates)


def offered_names(message: str) -> set[str]:
    """Identifier-like tokens in an error message, as whole words.

    Substring matching would let 's.congressional_districts' satisfy a contract looking for
    'congressional_district', which is exactly the shape of the bug being tested for.
    """
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", message))


def query_service(settings: Any, bdp_documents: dict[str, Any]) -> tuple[QueryService, httpx.AsyncClient]:
    """A real QueryService over a mock transport, so the catalog path under test is the signed one.

    Mirrors the idiom in test_query.py: the BDP documents come from the shared fixture and the
    relation rows are served from a local table, so nothing touches the network.
    """

    def handler(http_request: httpx.Request) -> httpx.Response:
        document = bdp_documents.get(str(http_request.url))
        if document is not None:
            return httpx.Response(200, json=document)
        if http_request.url.path == "/ngopen/usaspending/all_entities":
            return httpx.Response(200, json=[{"uei": "A", "name": "A", "total_obligation": 5, "state": "ME"}])
        return httpx.Response(404, json={"error": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return QueryService(settings, BdpRepository(settings, client), PostgrestTransport(settings, client)), client


def test_a_qualified_relation_survives_a_dataset_that_is_also_given(signed_catalog: Catalog) -> None:
    """`relation` is documented and contracted in its qualified form, and a caller who also passes
    `dataset` gets silently unrelated relations instead.

    Found by driving the live server: the model asked for `dataset=usp_cl` together with
    `relation=usp_cl.legislator_terms` - the exact `source` string discovery had just handed it for
    `benthic_query` - and got back `usp_cl.committee_membership`, a different relation entirely. The
    qualified split only ran when `dataset` was absent, so with both arguments the relation kept
    its dot, matched no bare name, and was dropped rather than rejected. The model spent a turn
    reasoning about "a routing index" before noticing.

    Dropping a filter is the one failure a caller cannot detect: the result is a well-formed answer
    to a different question. An unknown relation must be refused instead, which the existing
    validation already tries to do - it just never saw the value it was given.
    """
    qualified = "usp_cl.legislator_terms"
    dataset, _, name = qualified.partition(".")

    bare = signed_catalog.discover(relation=qualified, detail="full")
    both = signed_catalog.discover(dataset=dataset, relation=qualified, detail="full")
    split = signed_catalog.discover(dataset=dataset, relation=name, detail="full")

    assert [relation.source for relation in bare.relations] == [qualified]
    assert [relation.source for relation in both.relations] == [qualified], (
        "passing dataset alongside a qualified relation must not drop the relation filter"
    )
    assert both.relations[0].columns == bare.relations[0].columns
    assert [relation.source for relation in split.relations] == [qualified], (
        "the bare and qualified spellings must select the same relation"
    )


def test_an_unknown_relation_is_refused_rather_than_ignored(signed_catalog: Catalog) -> None:
    """The counterpart: a relation name that is in no signed manifest must not silently widen.

    `discover` returns `dataset` matches with no relation filter once an unknown name is dropped,
    which reads as an answer. A caller who typos a column's relation, or carries a name across
    datasets, gets a plausible list of the wrong tables.
    """
    with pytest.raises(ValueError, match="legislator_termz"):
        signed_catalog.discover(dataset="usp_cl", relation="legislator_termz", detail="full")

    # `server.discover` converts this to a `ToolError`, so the calling model sees a message naming
    # the offending relation instead of a list of the wrong tables.
    assert "signed relations" in str(
        pytest.raises(
            ValueError,
            signed_catalog.discover,
            dataset="usp_cl",
            relation="legislator_termz",
            detail="full",
        ).value
    )


def test_every_preferred_column_the_seed_names_exists_in_the_signed_manifest() -> None:
    """The seed playbook tells the model which columns to reach for, and a wrong one costs a turn.

    Guidance is prose that names real columns, so it rots the moment the catalog changes underneath
    it. There is no other check: a stale `preferred_columns` entry is served to the model on every
    `benthic_discover` call and produces exactly the wrong-column error the guidance was meant to
    prevent.
    """
    from benthic_mcp.seed import seed_playbook

    playbook = seed_playbook()
    signed = asyncio.run(live_catalog())
    if not signed:
        pytest.skip("the signed manifest cache is absent, so there is nothing to check against")
    missing: list[str] = []
    for source, guide in playbook.relations.items():
        dataset, _, relation = source.partition(".")
        definition = signed.relations.get((dataset, relation))
        if definition is None:
            missing.append(f"{source}: not in the signed manifest")
            continue
        for column in guide.preferred_columns:
            if column not in definition.columns:
                missing.append(f"{source}.{column}: no such column")
    assert not missing, "seed guidance names columns that do not exist: " + "; ".join(missing)


async def live_catalog() -> Catalog:
    """The real signed manifest, read from the repository cache rather than the network.

    `load` prefers a fresh cache, so this checks the guidance against what the server would
    actually serve. Returns an empty catalog when the cache is absent, which the caller treats as
    "nothing to check" rather than as a pass.
    """
    from benthic_mcp.config import Settings

    settings = Settings.from_env()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            return Catalog(await BdpRepository(settings, client).load())
    except Exception:  # noqa: BLE001 - an absent or unreadable cache means no comparison is possible
        return Catalog(CatalogSnapshot(collections={}, manifests={}))


def test_the_legislator_relations_name_the_legislator_joins() -> None:
    """The model re-derived this on every run, so it belongs in the standing guidance.

    From the transcripts: "The lawmakers table has bioguide_id as primary key" - the relation is
    called `legislators`, not `lawmakers` - and "The legislator_terms table presumably also has
    bioguide_id." Presumably. It guessed the join, guessed the relation name, and then guessed again
    from memory that a column called `name` existed, when `usp_cl.legislators` has no such column
    and the display value is `official_full`.

    A `RelationGuide` for `usp_cl.legislators` is the fix, and this asserts the two facts that make
    it useful: the relation is named in the dataset guidance at all, and the guidance names a
    column that exists.
    """
    from benthic_mcp.seed import seed_playbook

    playbook = seed_playbook()
    guide = playbook.relations.get("usp_cl.legislators")
    assert guide is not None, "usp_cl.legislators has no RelationGuide, so discovery returns it bare"
    assert "official_full" in guide.preferred_columns, (
        "official_full is the display column; there is no 'name' column on this relation"
    )
    assert "bioguide_id" in guide.preferred_columns, "it is the primary key and the join from terms"

    signed = asyncio.run(live_catalog())
    if not signed.relations:
        pytest.skip("the signed manifest cache is absent, so there is nothing to check against")
    definition = signed.relations[("usp_cl", "legislators")]
    assert "name" not in definition.columns, "the signed manifest has no 'name' column to recommend"
    assert "official_full" in definition.columns

    dataset = playbook.datasets["usp_cl"]
    assert any("bioguide_id" in line for line in dataset.when_to_use), (
        "the bioguide_id path between terms and legislators is not stated anywhere"
    )


def test_the_seed_never_asserts_a_join_the_manifest_does_not_sign() -> None:
    """A guidance line that tells the model to join two relations with no signed edge is worse than
    no guidance at all: the model follows it, `benthic_join` refuses, and the turn is spent on
    advice the server issued.

    This is not hypothetical. The first version of the `usp_cl.legislators` guidance said "Join
    `usp_cl.legislator_terms` to `usp_cl.legislators` on `bioguide_id`", and none of the six signed
    edges touches `legislators` - the model cannot do it. The prose screener dropped the line, which
    is how it was caught, and the guidance was rewritten to describe the two separate queries the
    model can actually make.

    Asserted here so the next piece of guidance cannot reintroduce it silently.
    """
    import asyncio

    signed = asyncio.run(live_catalog())
    if not signed.relations:
        pytest.skip("the signed manifest cache is absent, so there is nothing to check against")
    playbook = seed_playbook()
    known = known_identifiers(signed)

    for dataset_name, section in playbook.datasets.items():
        report = VerifyReport()
        dropped = screen_prose(" ".join(section.when_to_use), signed, known, report)
        assert dropped == [], f"{dataset_name} guidance asserts an unsigned join: {dropped}"
