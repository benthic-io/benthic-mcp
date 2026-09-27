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
