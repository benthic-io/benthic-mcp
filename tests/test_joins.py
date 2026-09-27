from typing import Any

import pytest

from benthic_mcp.catalog import Catalog
from benthic_mcp.errors import QueryValidationError
from benthic_mcp.joins import execute_joins
from benthic_mcp.models import JoinMode, JoinSpec, RelationSource, Reliability
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
