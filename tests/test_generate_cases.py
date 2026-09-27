"""The generator's harder and broader case families."""

from generate_cases import relation_traps, spread_across_datasets, spread_pairs, two_hop_chains

from benthic_mcp.bdp import CatalogSnapshot
from benthic_mcp.catalog import Catalog


def test_a_two_hop_chain_is_found_through_a_hub_relation(catalog: Catalog) -> None:
    # The fixture signs all_entities.uei -> sam_registrations.uei and
    # sam_registrations.duns -> bmf_organizations.ein, so sam_registrations is a hub.
    chains = two_hop_chains(catalog)

    assert chains
    left, middle, right, _, _ = chains[0]
    assert (left.dataset, left.name) == ("usaspending", "all_entities")
    assert (middle.dataset, middle.name) == ("samer", "sam_registrations")
    assert (right.dataset, right.name) == ("irs_ng", "bmf_organizations")


def test_spatial_paths_are_excluded_from_two_hop_chains() -> None:
    # A spatial hop has to go through benthic_rpc, so chaining it into a join question is wrong.
    def spatial_path(from_dataset: str, relation: str, to_dataset: str) -> dict:
        return {
            "from": {"dataset_name": from_dataset, "relation": relation, "column": "geom", "srid": 4326},
            "to": {"dataset_name": to_dataset, "relation": "hub", "column": "geom", "srid": 3857},
            "join_type": "spatial",
            "reliability": "reliable",
        }

    collection = {
        "join_paths": [
            spatial_path("a", "left", "b"),
            {
                "from": {"dataset_name": "b", "relation": "hub", "column": "geom", "srid": 4326},
                "to": {"dataset_name": "c", "relation": "right", "column": "geom", "srid": 3857},
                "join_type": "spatial",
                "reliability": "reliable",
            },
        ]
    }
    spatial = Catalog(CatalogSnapshot(collections={"ngopen": collection}, manifests={}))

    assert two_hop_chains(spatial) == []


def test_a_current_only_view_is_paired_with_a_historical_relation(catalog: Catalog) -> None:
    traps = relation_traps(catalog)

    assert traps
    correct, trap = traps[0]
    assert "current" in trap.name.lower()
    assert correct.name != trap.name
    assert correct.dataset == trap.dataset


def test_the_historical_relation_is_preferred_when_it_is_joined(catalog: Catalog) -> None:
    # Both legislator_terms and executive_terms are temporal; the one the signed catalog joins on is
    # the more likely subject of a question, so it wins the tiebreak.
    correct, _ = relation_traps(catalog)[0]

    assert correct.name == "legislator_terms"


def test_discovery_spread_covers_every_dataset_before_repeating(catalog: Catalog) -> None:
    queryable = [definition for definition in catalog.relations.values() if definition.queryable]
    datasets = {definition.dataset for definition in queryable}

    chosen = spread_across_datasets(queryable, len(datasets))

    assert {definition.dataset for definition in chosen} == datasets
    assert len(chosen) == len(datasets)


def test_discovery_spread_is_balanced_rather_than_catalog_order(catalog: Catalog) -> None:
    queryable = sorted(
        (definition for definition in catalog.relations.values() if definition.queryable),
        key=lambda definition: definition.name,
    )

    chosen = spread_across_datasets(queryable, 5)

    # Every dataset appears once before any dataset gets a second relation.
    first_pass = {definition.dataset for definition in chosen[: len({d.dataset for d in queryable})]}
    assert first_pass == {definition.dataset for definition in queryable}


def test_sequential_pairs_prefer_crossing_datasets(catalog: Catalog) -> None:
    left = catalog.relations[("usaspending", "all_entities")]
    right = catalog.relations[("samer", "sam_registrations")]
    crossing = (left, right, "uei")
    same = (left, catalog.relations[("usaspending", "prime_awards")], "recipient_uei")

    picked = spread_pairs([same, crossing], 1)

    assert picked == [crossing]


def test_spread_pairs_does_not_repeat_a_pair(catalog: Catalog) -> None:
    left = catalog.relations[("usaspending", "all_entities")]
    pair = (left, catalog.relations[("samer", "sam_registrations")], "uei")

    assert spread_pairs([pair, pair], 5) == [pair]


def test_the_generator_refuses_to_overwrite_the_hand_verified_golden_suite() -> None:
    # eval/golden is the harness's check on itself. Regenerating it from the harness would replace
    # that check with a derivative of the thing being checked.
    import argparse

    import anyio
    import pytest
    from generate_cases import GOLDEN_DIR, main_async

    args = argparse.Namespace(output_dir=str(GOLDEN_DIR))
    with pytest.raises(SystemExit, match="hand-verified"):
        anyio.run(main_async, args)
