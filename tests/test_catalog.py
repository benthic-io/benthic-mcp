import pytest

from benthic_mcp.catalog import Catalog
from benthic_mcp.errors import QueryValidationError


def test_discover_finds_new_manifest_relations(catalog: Catalog) -> None:
    cases = [
        ("form 990", "irs_ng", "form990_details"),
        ("subaward", "usaspending", "subawards"),
        ("executive term", "usp_cl", "executive_terms"),
        ("district spending", "usaspending", "mv_district_spending"),
    ]
    for query, dataset, relation in cases:
        result = catalog.discover(query=query, dataset=dataset, limit=8)
        assert relation in {item.relation for item in result.relations}, query


def test_discover_finds_recipient_relations(catalog: Catalog) -> None:
    result = catalog.discover(query="uei recipient")

    names = {(item.dataset, item.relation) for item in result.relations}
    assert ("usaspending", "all_entities") in names
    assert ("samer", "sam_registrations") in names


def test_discover_prioritizes_award_money_terms(catalog: Catalog) -> None:
    result = catalog.discover(query="organizations that got money in 2022", limit=3)

    assert result.relations[0].dataset == "usaspending"
    assert result.relations[0].relation == "prime_awards"
    assert {column.name for column in result.relations[0].columns} >= {
        "recipient_uei",
        "recipient_name",
        "award_amount",
        "action_date",
    }


def test_discover_prefers_historical_terms_for_historical_questions(catalog: Catalog) -> None:
    result = catalog.discover(query="historical representative for a district", limit=2)

    assert (result.relations[0].dataset, result.relations[0].relation) == ("usp_cl", "legislator_terms")


def test_discover_excludes_nonqueryable_relations(catalog: Catalog) -> None:
    result = catalog.discover(dataset="usaspending", query="lineage")

    assert all(item.relation != "lineage" for item in result.relations)


def test_resolve_relation_rejects_nonqueryable(catalog: Catalog) -> None:
    with pytest.raises(QueryValidationError, match="not queryable"):
        catalog.resolve_relation("usaspending", "lineage")


def test_find_join_requires_signed_path(catalog: Catalog) -> None:
    spending = catalog.resolve_relation("usaspending", "all_entities")
    sam = catalog.resolve_relation("samer", "sam_registrations")

    join = catalog.find_join(spending, sam, "uei", "uei")
    assert join.reliability.value == "reliable"

    with pytest.raises(QueryValidationError, match="not a signed BDP join path"):
        catalog.find_join(spending, sam, "name", "name")


def test_a_signed_hop_is_reachable_from_both_ends(catalog: Catalog) -> None:
    """A signed edge has to work whichever relation the caller is standing on.

    `join_signed_both` passed 6/13 and `join_wrong_edge` 10/12 across the observer's
    records, and a caller who names the two relations in the other order should not land in
    a different dead end. `find_join` is written to match either direction, so a hop that
    only resolves one way is a bug in the edge rather than in the caller, and this is the
    contract that says so.

    It is also the contract for `usp_cl.legislators.bioguide_id ->
    usp_cl.legislator_terms.bioguide_id`, which the model asked for twice and the
    collection did not carry. Until that edge is signed this fails, which is the point: the
    pipeline work and this test are two halves of one change.
    """
    legislators = catalog.resolve_relation("usp_cl", "legislators")
    terms = catalog.resolve_relation("usp_cl", "legislator_terms")

    forward = catalog.find_join(legislators, terms, "bioguide_id", "bioguide_id")
    # `bioguide_id` is an identifier and every one of the 45533 terms resolves to one of
    # the 12768 legislators, so this earns `reliable`. A bare `district` number does not:
    # that edge is `partial` because it is only an identity when paired with state.
    assert forward.reliability.value == "reliable"
    assert str(forward.join_type) == "identifier"

    reverse = catalog.find_join(terms, legislators, "bioguide_id", "bioguide_id")
    assert reverse.reliability.value == "reliable"
    assert (reverse.from_relation, reverse.from_column) == (
        "legislators",
        "bioguide_id",
    ), "the reversed edge must point back at the relation the caller is standing on"


def test_a_signed_hop_is_advertised_to_discovery(catalog: Catalog) -> None:
    """Signing an edge is only useful if discovery hands it to the model.

    A hop in the manifest that `find_join` accepts but discovery does not surface is a
    silent dead end: the model never learns the path exists, so it asks for something
    else and gets refused for the wrong reason. This is the failure the combination suite
    found when it named seven hops that were missing, three of which turned out to be
    missing from discovery rather than from the collection.
    """
    terms = catalog.resolve_relation("usp_cl", "legislator_terms")
    # Qualified names, like every other caller. `join_paths` and `join_graph` agree on that
    # shape, and passing bare names returns [] rather than raising, which is a silent wrong
    # answer rather than a caught mistake.
    routes = catalog.join_paths("usp_cl.legislator_terms", "usp_cl.legislators")

    assert routes, (
        "a signed legislators/legislator_terms hop must be advertised between the two "
        "relations, or the model cannot find the path the collection actually carries"
    )
    edges = [edge for route in routes for edge in route]
    assert any(edge.left_column == "bioguide_id" and edge.right == "usp_cl.legislators" for edge in edges), (
        f"expected a bioguide_id edge from legislator_terms to legislators, got {edges}"
    )
    assert terms.dataset == "usp_cl"
