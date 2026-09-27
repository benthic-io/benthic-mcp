"""Response size is a performance property, so it gets a test like any other.

Every byte a tool returns is re-sent to the model on each remaining turn, so response size is
multiplied by conversation length rather than added to it. A measured case went 3,026 -> 7,315 ->
10,818 -> 12,638 -> 17,342 prompt tokens over five turns. Adding a field to ColumnInfo or
SourceMetadata therefore costs far more than its own length, which is invisible in a diff.

What this file does *not* assert is that any particular field is unnecessary. Removing
ColumnInfo.native_type, srid and unit plus the per-source manifest_hash cut a discovery response by
31% and moved the whole run's prompt tokens by 0.6%, while tuning accuracy fell from 23/25 to 18/25.
The 0.6% is the instructive part: response bytes are dominated by data rows, not schema metadata, so
trimming the schema buys nothing measurable and the fields may be carrying weight the model relies
on. The budget is kept because it is cheap and would catch a genuinely large addition, not because
it justifies a trim.
"""

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from benthic_mcp.bdp import CatalogSnapshot
from benthic_mcp.catalog import _INLINE_COLUMN_LIMIT, Catalog
from tests.factories import make_collection, make_manifest, relation

# Measured: one column entry serialises to 57 bytes and the marginal cost of an extra column is 95
# bytes with separators. This ceiling is roughly double the measured marginal cost, so it allows a
# short extra field and would still fail on anything on the scale of a 64-character hash per column.
MAX_MARGINAL_BYTES_PER_COLUMN = 200
MAX_BYTES_PER_RELATION = 3_000
COLUMN_COUNT = _INLINE_COLUMN_LIMIT + 18


@pytest.fixture
def catalog_with(private_key: Ed25519PrivateKey) -> Catalog:
    """Wider than the inline limit, so a summary and a full listing are genuinely different."""
    columns = [(f"field_{index:03d}", "string") for index in range(COLUMN_COUNT)]
    manifests = {
        "usaspending": make_manifest(
            private_key,
            "usaspending",
            [relation("sample", columns, description="A sample relation")],
            "https://benthic.io/ngopen/usaspending/",
        )
    }
    return Catalog(
        CatalogSnapshot(collections={"ngopen": make_collection(private_key, manifests)}, manifests=manifests)
    )


def relation_sizes(catalog: Catalog, **kwargs) -> list[int]:
    payload = catalog.discover(**kwargs).model_dump(mode="json", exclude_none=True)
    return [len(json.dumps(item)) for item in payload["relations"]]


def summary_sizes(catalog: Catalog) -> list[int]:
    return relation_sizes(catalog, query="field_001", relation="sample")


def full_sizes(catalog: Catalog) -> list[int]:
    return relation_sizes(catalog, query="field_001", relation="sample", detail="full")


def test_a_discovery_response_stays_within_its_per_relation_budget(catalog_with: Catalog) -> None:
    for size in summary_sizes(catalog_with):
        assert size <= MAX_BYTES_PER_RELATION, f"relation payload {size} bytes"


def test_listing_every_column_costs_more_than_listing_twelve(catalog_with: Catalog) -> None:
    """The property that makes response size worth thinking about: cost is per column, per turn."""
    summary, full = summary_sizes(catalog_with), full_sizes(catalog_with)

    assert len(summary) == len(full) == 1
    assert full[0] > summary[0]


def test_the_marginal_cost_of_a_column_stays_bounded(catalog_with: Catalog) -> None:
    summary, full = summary_sizes(catalog_with), full_sizes(catalog_with)
    extra = COLUMN_COUNT - _INLINE_COLUMN_LIMIT
    marginal = (full[0] - summary[0]) / extra

    assert marginal <= MAX_MARGINAL_BYTES_PER_COLUMN, f"marginal cost {marginal:.0f} bytes per column"


def test_the_full_listing_of_thirty_columns_stays_bounded(catalog_with: Catalog) -> None:
    """One relation, fully listed, is the largest schema response the server can produce."""
    full = full_sizes(catalog_with)

    assert full[0] <= MAX_BYTES_PER_RELATION
