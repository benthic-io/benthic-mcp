"""The golden suite is the harness's own check on itself.

A scorer bug once understated five discovery cases, and the resulting trend was read as a playbook
result. These cases are the guard against repeating that: each expected value is hand-verified
against the signed catalog or a live call, and a failure here is a bug in the harness, the scorer,
or the tool surface rather than a finding about the playbook.
"""

import json
from pathlib import Path
from typing import Any

import pytest

GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden" / "questions.json"

KNOWN_TOOLS = {
    "benthic_discover",
    "benthic_query",
    "benthic_join",
    "benthic_rpc",
    "benthic_playbook",
    "benthic_report",
}


def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def cases() -> list[dict[str, Any]]:
    return golden()["cases"]


def case(case_id: str) -> dict[str, Any]:
    return next(item for item in cases() if item["id"] == case_id)


def test_the_golden_suite_is_small_and_covers_several_capabilities() -> None:
    # A self-check that needs a long run stops being run, so it is deliberately tiny.
    assert 3 <= len(cases()) <= 8
    assert len({item["capability"] for item in cases()}) >= 3


def test_the_golden_suite_states_its_contract() -> None:
    # Every case must pass with the seed alone, so a case that fails for capability reasons would
    # leave the suite permanently red and it would stop being read.
    assert "must pass with the seed playbook alone" in golden()["metadata"]["contract"]


def test_golden_case_ids_are_unique() -> None:
    ids = [item["id"] for item in cases()]

    assert len(ids) == len(set(ids))


def test_golden_ids_are_namespaced() -> None:
    # Regeneration is blocked by directory, and this makes an accidental copy obvious in a diff.
    assert all(item["id"].startswith("golden_") for item in cases())


def test_every_golden_case_only_requires_real_tools() -> None:
    for item in cases():
        assert item["required_tools"] or item.get("required_tools_any"), item["id"]
        assert set(item["required_tools"]) <= KNOWN_TOOLS, item["id"]
        for route in item.get("required_tools_any", []):
            assert route and set(route) <= KNOWN_TOOLS, item["id"]


def test_a_case_with_alternate_routes_pins_at_least_two_of_them() -> None:
    # One route is just a requirement with extra syntax, and would hide which tools are acceptable.
    for item in cases():
        if item.get("required_tools_any"):
            assert len(item["required_tools_any"]) >= 2, item["id"]


def test_every_golden_case_states_what_it_expects() -> None:
    # The scorer's checks are all driven by `expected`, so an empty one silently always passes.
    for item in cases():
        assert item.get("expected"), item["id"]
        assert item.get("question", "").strip(), item["id"]


def test_a_held_back_lesson_cannot_claim_a_relation_the_catalog_lacks() -> None:
    """Re-verified against the signed catalog, so a catalog change cannot leave the suite stale."""
    catalog = _catalog()
    if catalog is None:
        pytest.skip("signed catalog unavailable")
    for item in cases():
        relation = item["expected"].get("relation")
        if not relation:
            continue
        dataset, _, name = str(relation).partition(".")
        assert (dataset, name) in catalog.relations, item["id"]


def test_golden_discovery_columns_still_exist() -> None:
    catalog = _catalog()
    if catalog is None:
        pytest.skip("signed catalog unavailable")
    expected = case("golden_discover_committee_membership")["expected"]
    dataset, _, name = str(expected["relation"]).partition(".")
    columns = set(catalog.relations[(dataset, name)].columns)

    assert set(expected["columns"]) <= columns


def test_the_golden_unsigned_pair_is_still_unsigned() -> None:
    """If the catalog ever signs this pair the case is wrong, and a rejection would be a bug."""
    catalog = _catalog()
    if catalog is None:
        pytest.skip("signed catalog unavailable")
    left = case("golden_unsigned_agency_lookup")
    assert left["expected"]["must_reject"] is True

    signed_relations = {name for pair in catalog.signed_join_pairs() for name in (pair[0], pair[2])}

    assert "usaspending.agency_lookup" not in signed_relations


def test_the_golden_join_path_is_still_signed() -> None:
    catalog = _catalog()
    if catalog is None:
        pytest.skip("signed catalog unavailable")
    path = case("golden_signed_uei_join_empty_right")["expected"]["join_path"]
    signed = {
        (left, left_column, right, right_column)
        for left, left_column, right, right_column in catalog.signed_join_pairs()
    }

    assert (path["left"], path["left_column"], path["right"], path["right_column"]) in signed


def test_the_golden_trap_still_points_at_a_present_day_view() -> None:
    """The trap is not a golden case, but the catalog fact it rests on is still worth pinning."""
    catalog = _catalog()
    if catalog is None:
        pytest.skip("signed catalog unavailable")
    trap_dataset, trap_name = "usp_cl", "mv_current_lawmakers"
    trap = catalog.relations[(trap_dataset, trap_name)]
    historical = catalog.relations[("usp_cl", "legislator_terms")]

    assert "current" in trap_name
    assert "term_end" in trap.columns
    assert {"term_start", "term_end"} <= set(historical.columns)


def _catalog() -> Any:
    """The real signed catalog, or None when it cannot be loaded without a network refresh."""
    import anyio

    from benthic_mcp.config import Settings
    from benthic_mcp.service import BenthicService

    async def load() -> Any:
        service = BenthicService(Settings.from_env())
        try:
            return (await service.playbook()).catalog
        finally:
            await service.close()

    try:
        return anyio.run(load)
    except Exception:  # noqa: BLE001 - absence of a catalog is a skip, not a failure
        return None
