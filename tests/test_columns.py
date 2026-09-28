"""The unknown-column dead end, and the escape hatch that closes it.

Discovery lists at most 12 columns per relation and 21 of the 119 signed relations have more than
40, one of them 374. Combined with an error that used to name only the rejected column, a model
that guessed wrong had no way to learn the real name except guessing again. The eval traces show
that loop consuming the turn budget, and prose telling the model to check its columns cannot break
it because the information was not reachable.
"""

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from benthic_mcp.bdp import CatalogSnapshot
from benthic_mcp.catalog import _INLINE_COLUMN_LIMIT, Catalog
from benthic_mcp.errors import QueryValidationError
from tests.factories import make_collection, make_manifest, relation

ALL_ENTITIES = "usaspending.all_entities"
PRIME_AWARDS = "usaspending.prime_awards"


@pytest.fixture
def wide_catalog(private_key: Ed25519PrivateKey) -> Catalog:
    """A dataset with a relation wider than the inline limit, which the shared fixture lacks."""
    columns = [(f"field_{index:03d}", "string") for index in range(_INLINE_COLUMN_LIMIT + 9)]
    manifests = {
        "usaspending": make_manifest(
            private_key,
            "usaspending",
            [
                relation("wide", columns),
                relation("narrow", [("id", "string"), ("state", "string"), ("label", "string")]),
            ],
            "https://benthic.io/ngopen/usaspending/",
        )
    }
    return Catalog(
        CatalogSnapshot(collections={"ngopen": make_collection(private_key, manifests)}, manifests=manifests)
    )


def definition_of(catalog: Catalog, source: str):
    dataset, _, name = source.partition(".")
    return catalog.relations[(dataset, name)]


def test_a_plural_guess_resolves_to_the_signed_singular(catalog: Catalog) -> None:
    definition = definition_of(catalog, ALL_ENTITIES)

    assert catalog.column_candidates(definition, "total_obligations") == ["total_obligation"]


def test_the_error_names_the_candidate_rather_than_leaving_a_dead_end(catalog: Catalog) -> None:
    definition = definition_of(catalog, ALL_ENTITIES)

    with pytest.raises(QueryValidationError) as caught:
        catalog.validate_columns(definition, ["total_obligations"])

    assert "total_obligation" in str(caught.value)


def test_several_candidates_are_offered_as_a_list_and_none_is_guessed(catalog: Catalog) -> None:
    definition = definition_of(catalog, PRIME_AWARDS)

    with pytest.raises(QueryValidationError) as caught:
        catalog.validate_columns(definition, ["recipient"])

    message = str(caught.value)
    assert "one of" in message
    # Every name offered is a signed column, and the one closest to the guess leads.
    assert all(name in definition.columns for name in definition.columns if "recipient" in name)
    assert "recipient_congressional_district" in message


def test_a_guess_matching_several_columns_still_offers_only_signed_names(catalog: Catalog) -> None:
    definition = definition_of(catalog, PRIME_AWARDS)
    offered = catalog.column_candidates(definition, "recipient")

    assert offered and set(offered) <= set(definition.columns)
    assert len(offered) < len(definition.columns)


def test_a_candidate_can_never_be_a_column_the_manifest_does_not_have(catalog: Catalog) -> None:
    definition = definition_of(catalog, ALL_ENTITIES)

    candidates = catalog.column_candidates(definition, "total_obligations")

    assert set(candidates) <= set(definition.columns)


def test_a_wildly_wrong_guess_offers_no_candidate_rather_than_a_rubbish_one(catalog: Catalog) -> None:
    definition = definition_of(catalog, ALL_ENTITIES)

    assert catalog.column_candidates(definition, "xyzzy_nonsense") == []


def test_an_empty_guess_offers_nothing(catalog: Catalog) -> None:
    assert catalog.column_candidates(definition_of(catalog, ALL_ENTITIES), "   ") == []


def test_a_valid_column_raises_nothing(catalog: Catalog) -> None:
    catalog.validate_columns(definition_of(catalog, ALL_ENTITIES), ["uei", "total_obligation"])


def test_a_wide_relation_is_told_to_ask_for_the_full_listing(wide_catalog: Catalog) -> None:
    with pytest.raises(QueryValidationError) as caught:
        wide_catalog.validate_columns(definition_of(wide_catalog, "usaspending.wide"), ["xyzzy_nonsense"])

    message = str(caught.value)
    assert "detail='full'" in message
    assert str(len(definition_of(wide_catalog, "usaspending.wide").columns)) in message


def test_a_narrow_relation_is_told_to_use_discovery_plainly(catalog: Catalog) -> None:
    with pytest.raises(QueryValidationError) as caught:
        catalog.validate_columns(definition_of(catalog, ALL_ENTITIES), ["xyzzy_nonsense"])

    assert "detail='full'" not in str(caught.value)


def test_discovery_caps_the_inline_column_list(wide_catalog: Catalog) -> None:
    result = wide_catalog.discover(relation="wide")

    assert len(result.relations[0].columns) == _INLINE_COLUMN_LIMIT
    assert result.relations[0].columns_truncated is True


def test_the_full_listing_returns_every_column_and_claims_no_truncation(wide_catalog: Catalog) -> None:
    result = wide_catalog.discover(relation="wide", detail="full")

    assert len(result.relations[0].columns) == _INLINE_COLUMN_LIMIT + 9
    assert result.relations[0].columns_truncated is False


def test_the_full_listing_returns_one_relation_so_the_response_stays_bounded(wide_catalog: Catalog) -> None:
    result = wide_catalog.discover(query="field_001", relation="wide", detail="full")

    assert len(result.relations) == 1


def test_the_full_listing_applies_to_the_best_match_when_no_relation_is_named(wide_catalog: Catalog) -> None:
    # Refusing this made the model spend whole turns retrying the identical call, which is the dead
    # end the parameter exists to remove. A caller with a query but no relation is asking about the
    # best match for that query.
    result = wide_catalog.discover(query="field_001", dataset="usaspending", detail="full")

    assert len(result.relations) == 1
    assert result.relations[0].columns_truncated is False


def test_the_full_listing_returns_nothing_rather_than_everything_when_nothing_matches(wide_catalog: Catalog) -> None:
    result = wide_catalog.discover(query="no such thing", detail="full")

    assert result.relations == []


def test_the_summary_response_is_unchanged_by_the_new_parameter(wide_catalog: Catalog) -> None:
    # The default must reproduce the existing response, since every existing caller relies on it.
    assert wide_catalog.discover(query="", relation="wide") == wide_catalog.discover(
        query="", relation="wide", detail="summary"
    )


def test_the_inline_view_plus_the_full_listing_covers_every_signed_column(wide_catalog: Catalog) -> None:
    """The property that keeps the inline cap from ever making a column unreachable."""
    definition = definition_of(wide_catalog, "usaspending.wide")

    inline = {c.name for c in wide_catalog.discover(relation="wide").relations[0].columns}
    listed = {c.name for c in wide_catalog.discover(relation="wide", detail="full").relations[0].columns}

    assert listed == set(definition.columns)
    assert inline < listed
