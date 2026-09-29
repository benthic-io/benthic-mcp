from typing import Any

import pytest

from benthic_mcp.catalog import Catalog
from benthic_mcp.errors import QueryValidationError
from benthic_mcp.joins import _join_key, execute_joins
from benthic_mcp.models import JoinCondition, JoinMode, JoinSpec, RelationSource, Reliability
from benthic_mcp.postgrest import FetchedSource


def _source(
    catalog: Catalog,
    alias: str,
    dataset: str,
    relation: str,
    rows: list[dict[str, Any]],
) -> FetchedSource:
    source = RelationSource(alias=alias, dataset=dataset, relation=relation)
    return FetchedSource(
        source=source,
        definition=catalog.resolve_relation(dataset, relation),
        rows=rows,
        request_url="https://benthic.io/example",
        truncated=False,
    )


def test_reliable_join_namespaces_columns(catalog: Catalog) -> None:
    spending = _source(
        catalog,
        "awards",
        "usaspending",
        "all_entities",
        [{"uei": "A", "name": "Award recipient"}],
    )
    sam = _source(
        catalog,
        "sam",
        "samer",
        "sam_registrations",
        [{"uei": "A", "name": "SAM entity"}],
    )

    result = execute_joins(
        catalog,
        [spending, sam],
        [JoinSpec(left_alias="awards", right_alias="sam", left_column="uei", right_column="uei")],
        {Reliability.RELIABLE},
        100,
    )

    assert result.rows == [
        {
            "awards.uei": "A",
            "awards.name": "Award recipient",
            "sam.uei": "A",
            "sam.name": "SAM entity",
        }
    ]
    assert result.metadata[0].reliability == Reliability.RELIABLE


def test_heuristic_join_requires_opt_in_and_warns(catalog: Catalog) -> None:
    sam = _source(catalog, "sam", "samer", "sam_registrations", [{"duns": "1", "uei": "A"}])
    irs = _source(catalog, "irs", "irs_ng", "bmf_organizations", [{"ein": "1", "org_name": "Nonprofit"}])
    spec = JoinSpec(left_alias="sam", right_alias="irs", left_column="duns", right_column="ein")

    with pytest.raises(QueryValidationError, match="not enabled"):
        execute_joins(catalog, [sam, irs], [spec], {Reliability.RELIABLE}, 100)

    result = execute_joins(catalog, [sam, irs], [spec], {Reliability.HEURISTIC}, 100)
    assert result.metadata[0].reliability == Reliability.HEURISTIC
    assert any("Heuristic" in warning for warning in result.metadata[0].warnings)


def test_left_join_retains_unmatched_rows(catalog: Catalog) -> None:
    spending = _source(catalog, "awards", "usaspending", "all_entities", [{"uei": "A"}, {"uei": "B"}])
    sam = _source(catalog, "sam", "samer", "sam_registrations", [{"uei": "A"}])
    spec = JoinSpec(
        left_alias="awards",
        right_alias="sam",
        left_column="uei",
        right_column="uei",
        mode=JoinMode.LEFT,
    )

    result = execute_joins(catalog, [spending, sam], [spec], {Reliability.RELIABLE}, 100)
    assert len(result.rows) == 2
    assert result.rows[1]["sam.uei"] is None


def test_unsigned_join_is_rejected(catalog: Catalog) -> None:
    spending = _source(catalog, "awards", "usaspending", "all_entities", [{"name": "A"}])
    sam = _source(catalog, "sam", "samer", "sam_registrations", [{"name": "A"}])
    spec = JoinSpec(left_alias="awards", right_alias="sam", left_column="name", right_column="name")

    with pytest.raises(QueryValidationError, match="not a signed BDP join path"):
        execute_joins(catalog, [spending, sam], [spec], {Reliability.RELIABLE}, 100)


def _mismatched_catalog() -> Catalog:
    """A catalog declaring the types the real signed manifest declares.

    The shared fixture types legislator_terms.district as string, so it agrees with
    all_entities.congressional_district and could never have surfaced this. Production declares
    district as an integer, which is the whole problem.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from benthic_mcp.bdp import CatalogSnapshot
    from benthic_mcp.playbook import relation_hints
    from benthic_mcp.seed import seed_playbook
    from tests.factories import make_collection, make_manifest, relation

    key = Ed25519PrivateKey.generate()
    manifests = {
        "usaspending": make_manifest(
            key,
            "usaspending",
            [relation("all_entities", [("uei", "string"), ("state", "string"), ("congressional_district", "string")])],
            "https://benthic.io/ngopen/usaspending/",
        ),
        "usp_cl": make_manifest(
            key,
            "usp_cl",
            [relation("legislator_terms", [("term_id", "integer"), ("district", "integer"), ("state", "string")])],
            "https://benthic.io/ngopen/usp_cl/",
        ),
    }
    collection = make_collection(
        key,
        manifests,
        [
            {
                "from": {
                    "dataset_name": "usaspending",
                    "relation": "all_entities",
                    "column": "congressional_district",
                },
                "to": {"dataset_name": "usp_cl", "relation": "legislator_terms", "column": "district"},
                "join_type": "identifier",
                "reliability": "partial",
                "notes": "Must pair with state",
            }
        ],
    )
    return Catalog(
        CatalogSnapshot(collections={"ngopen": collection}, manifests=manifests),
        relation_hints=relation_hints(seed_playbook()),
    )


def test_a_zero_padded_text_key_matches_the_integer_it_represents() -> None:
    """The signed edge that never worked.

    usaspending.all_entities.congressional_district is text and zero-padded, '03';
    usp_cl.legislator_terms.district is an integer, 3. Join keys are compared in Python, so nothing
    coerced them, the equijoin matched nothing, and every call to this signed path returned 0 rows.
    The suite scored the route as correct and never read the result, so it went unreported for as
    long as the harness had been running.
    """
    assert _join_key({"left.congressional_district": "03"}, "left", ["congressional_district"], coerce=True) == (
        _join_key({"right.district": 3}, "right", ["district"], coerce=True)
    )


def test_coercion_does_not_equate_a_word_with_a_number() -> None:
    assert _join_key({"left.x": "abc"}, "left", ["x"], coerce=True) != _join_key(
        {"right.y": 0}, "right", ["y"], coerce=True
    )


def test_keys_are_left_alone_when_both_sides_declare_the_same_type() -> None:
    """Coercion is for a declared mismatch, not a general normalisation."""
    assert _join_key({"left.x": "03"}, "left", ["x"]) != _join_key({"right.x": 3}, "right", ["x"])


def test_a_type_mismatch_on_a_signed_join_is_reported_rather_than_hidden() -> None:
    """A 0-row result the catalog can explain should say so, not look like absence of data."""
    from benthic_mcp.joins import _type_coercion

    catalog = _mismatched_catalog()
    note = _type_coercion(
        catalog.resolve_relation("usaspending", "all_entities"),
        "congressional_district",
        catalog.resolve_relation("usp_cl", "legislator_terms"),
        "district",
    )

    assert note is not None
    assert "different types" in note


def test_matching_types_produce_no_coercion_note() -> None:
    from benthic_mcp.joins import _type_coercion

    catalog = _mismatched_catalog()
    definition = catalog.resolve_relation("usp_cl", "legislator_terms")

    assert _type_coercion(definition, "term_id", definition, "term_id") is None


def test_the_join_reports_the_type_mismatch_in_its_warnings() -> None:
    """Through execute_joins, because a helper-only test would not catch a caller that forgets to
    thread the flag through."""
    catalog = _mismatched_catalog()
    # execute_joins prefixes rows with the alias itself, so the fixtures stay unprefixed.
    spending = _source(
        catalog, "left", "usaspending", "all_entities", [{"uei": "A", "state": "MD", "congressional_district": "03"}]
    )
    terms = _source(catalog, "right", "usp_cl", "legislator_terms", [{"term_id": 1, "district": 3, "state": "MD"}])
    spec = JoinSpec(
        left_alias="left",
        right_alias="right",
        left_column="congressional_district",
        right_column="district",
        mode=JoinMode.INNER,
        extra_conditions=[JoinCondition(left_column="state", right_column="state")],
    )

    result = execute_joins(catalog, [spending, terms], [spec], {Reliability.PARTIAL}, 100)

    assert result.rows, "the zero-padded text key should now match the integer it represents"
    assert "different types" in " ".join(result.metadata[0].warnings or [])


def test_a_context_conditions_column_is_actually_fetched(catalog: Catalog) -> None:
    """A partial signed join whose context column is not selected can never return a row.

    The select lists were built from the key column and the filter columns; a context condition's
    columns were parsed afterwards and added to neither. The join key became (key, None),
    execute_joins drops every row on a null component, and the result was 0 rows for a path the
    catalog says is populated - indistinguishable from a join that is broken, which is what made
    this worth a contract rather than a test of the helper.
    """
    from benthic_mcp.query import build_single_join

    request = build_single_join(
        question="probe",
        left_source="usp_cl.legislator_terms",
        right_source="usaspending.all_entities",
        left_column="district",
        right_column="congressional_district",
        left_where=["state=eq.MD"],
        context_conditions=["state=state"],
    )

    by_alias = {source.alias: source for source in request.sources}
    condition = request.joins[0].extra_conditions[0]

    assert condition.left_column in by_alias["left"].select
    assert condition.right_column in by_alias["right"].select
