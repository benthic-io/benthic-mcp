"""Response size is a performance property, so it gets a test like any other.

Every byte a tool returns is re-sent to the model on each remaining turn, so response size is
multiplied by conversation length rather than added to it. A measured case went 3,026 -> 7,315 ->
10,818 -> 12,638 -> 17,342 prompt tokens over five turns, and discovery responses were larger than
the data queries they preceded. Adding a field to ColumnInfo or SourceMetadata therefore costs far
more than its own length, which is invisible in a diff.

These bounds are per column, because that is the multiplier. They are set with headroom above the
current measurement so a genuinely useful field can be added, but a field that duplicates the
manifest into the response cannot come back unnoticed.
"""

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from benthic_mcp.bdp import CatalogSnapshot
from benthic_mcp.catalog import _INLINE_COLUMN_LIMIT, Catalog
from tests.factories import make_collection, make_manifest, relation

# Measured: one column entry serialises to 57 bytes, 30 columns to 1,992, and the marginal cost of an
# extra column is 95 bytes with separators. The ceiling leaves room for one short extra field and is
# still well under what a 64-character manifest hash per source or per column would add.
MAX_BYTES_PER_COLUMN = 120
MAX_BYTES_PER_RELATION = 1_500
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


def sizes(catalog: Catalog, **kwargs) -> tuple[list[int], int]:
    result = catalog.discover(**kwargs)
    payload = result.model_dump(mode="json", exclude_none=True)
    return [len(json.dumps(item)) for item in payload["relations"]], len(json.dumps(payload))


def summary_sizes(catalog: Catalog) -> tuple[list[int], int]:
    return sizes(catalog, query="field_001", relation="sample")


def full_sizes(catalog: Catalog) -> tuple[list[int], int]:
    return sizes(catalog, query="field_001", relation="sample", detail="full")


def test_a_discovery_response_stays_within_its_per_column_budget(catalog_with: Catalog) -> None:
    per_relation, _ = summary_sizes(catalog_with)

    assert per_relation
    for size in per_relation:
        assert size <= MAX_BYTES_PER_RELATION, f"relation payload {size} bytes"


def test_listing_every_column_costs_more_than_listing_twelve(catalog_with: Catalog) -> None:
    """The property that makes trimming worthwhile: cost is per column, paid once per turn."""
    summary, _ = summary_sizes(catalog_with)
    full, _ = full_sizes(catalog_with)

    assert len(summary) == len(full) == 1
    assert full[0] > summary[0]


def test_the_full_listing_grows_by_roughly_the_marginal_cost_per_column(catalog_with: Catalog) -> None:
    summary, _ = summary_sizes(catalog_with)
    full, _ = full_sizes(catalog_with)
    extra = COLUMN_COUNT - _INLINE_COLUMN_LIMIT

    marginal = (full[0] - summary[0]) / extra
    assert 20 <= marginal <= MAX_BYTES_PER_COLUMN, f"marginal cost {marginal:.0f} bytes per column"


def test_no_null_valued_column_metadata_is_serialised(catalog_with: Catalog) -> None:
    """`"srid": null` costs about as much as a real value and told the model nothing."""
    result = catalog_with.discover(query="field_001", relation="sample", detail="full")
    column = result.relations[0].columns[0].model_dump(mode="json")

    assert set(column) == {"name", "type", "nullable", "description"}


def test_source_metadata_carries_no_manifest_hash() -> None:
    """It was served per source in every query result and read by nothing.

    A 64-character hash plus its key is about 85 bytes per source, re-sent on every remaining turn.
    Signature verification happens when the manifest loads, and the hash is what
    Catalog.fingerprint() uses for staleness, so neither needs it in the response.
    """
    from benthic_mcp.models import SourceMetadata

    fields = set(SourceMetadata.model_fields)

    assert fields == {"alias", "source", "row_count", "complete"}
