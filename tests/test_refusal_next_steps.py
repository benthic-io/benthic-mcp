"""A refusal must point at the next step, or it dead-ends the caller.

`check_pipeline_provenance.py`'s cousin problem: a message that names a failure and nothing else
costs the user the turns they spend guessing what to do. These contracts assert the high-frequency
dead-ends from a full audit of every refusal string carry a concrete next step.
"""

from __future__ import annotations

import pytest

from benthic_mcp.catalog import Catalog
from benthic_mcp.errors import QueryValidationError
from benthic_mcp.query import _numeric


def test_a_missing_relation_points_at_discover(catalog: Catalog) -> None:
    """`Relation X is not in the signed BDP manifest` told the caller it failed and nothing else.

    The discover-side copy already says "call discover without a relation to see the signed relations"
    (catalog.py:298-301). The query-side copy did not.
    """
    with pytest.raises(QueryValidationError, match="benthic_discover"):
        catalog.resolve_relation("usaspending", "no_such_relation")


def test_a_nonqueryable_relation_points_at_discover(catalog: Catalog) -> None:
    with pytest.raises(QueryValidationError, match="benthic_discover"):
        catalog.resolve_relation("usaspending", "lineage")


def test_a_non_numeric_aggregate_offers_the_exit(catalog: Catalog) -> None:
    """`Aggregate X requires numeric values` states the failure and gives no way out.

    The exit is either to count rows instead of summing them, or to sum a column that is numeric. The
    message said neither, so a caller hitting it had to guess which one was meant.
    """
    with pytest.raises(QueryValidationError, match="count"):
        _numeric("not a number", "sum")
