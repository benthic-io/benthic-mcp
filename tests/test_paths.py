"""Joining was a search the caller had to re-phrase. It is now a lookup keyed on two relations.

The measured failure this exists to fix: asked for a route between two datasets, a model called
benthic_discover five or six times and never called benthic_query. Discovery was returning the
correct path every time, so the information was not the problem. A search can be rephrased, so a
request for a path that does not exist comes back indistinguishable from one that was worded badly,
and the only available move is to ask again. Two exact relation names cannot be rephrased.

The signed graph is a handful of edges, so every answer here is a pure function of the signed
catalog. There is nothing to memoise; the problem was addressing, not memory.

The shared fixture signs the same shape as production: a reliable identifier path, a heuristic one,
and a partial one, plus the two-hop chain through samer.sam_registrations.
"""

from benthic_mcp.catalog import JoinDefinition
from benthic_mcp.models import Reliability
from benthic_mcp.playbook import build_path

ALL = "usaspending.all_entities"
SAM = "samer.sam_registrations"
BMF = "irs_ng.bmf_organizations"
TERMS = "usp_cl.legislator_terms"
ISOLATED = "usaspending.agency_lookup"


def add(catalog, left: str, left_column: str, right: str, right_column: str, join_type: str, reliability: str) -> None:
    catalog.joins.append(
        JoinDefinition(
            from_dataset=left.split(".")[0],
            from_relation=left.split(".")[1],
            from_column=left_column,
            to_dataset=right.split(".")[0],
            to_relation=right.split(".")[1],
            to_column=right_column,
            join_type=join_type,
            reliability=Reliability(reliability),
            notes=None,
        )
    )


def pairs_of(catalog, left: str, right: str, max_hops: int = 2) -> list[list[tuple[str, str, str, str]]]:
    return [[hop.as_pair() for hop in route] for route in catalog.join_paths(left, right, max_hops)]


def test_a_direct_signed_edge_is_found(catalog) -> None:
    assert pairs_of(catalog, ALL, SAM) == [[(ALL, "uei", SAM, "uei")]]


def test_a_two_hop_route_is_found_through_the_hub(catalog) -> None:
    routes = pairs_of(catalog, ALL, BMF)

    assert [[hop[0] for hop in route] for route in routes] == [[ALL, SAM]]


def test_the_direct_route_is_listed_before_a_longer_one_when_both_exist(catalog) -> None:
    add(catalog, ALL, "duns", BMF, "ein", "heuristic", "heuristic")

    routes = pairs_of(catalog, ALL, BMF)

    assert sorted(len(route) for route in routes) == [1, 2]
    assert len(routes[0]) == 1


def test_direction_is_never_a_dead_end(catalog) -> None:
    # The same signed edge, walked from the other end, so the hops are oriented the other way round.
    forward = pairs_of(catalog, ALL, SAM)
    backward = pairs_of(catalog, SAM, ALL)

    assert [len(route) for route in forward] == [len(route) for route in backward]
    assert forward[0][0] == (ALL, "uei", SAM, "uei")
    assert backward[0][0] == (SAM, "uei", ALL, "uei")


def test_each_hop_is_oriented_so_it_can_be_called_verbatim(catalog) -> None:
    hop = catalog.join_paths(SAM, ALL)[0][0]

    assert hop.left == SAM
    assert hop.right == ALL
    assert hop.left_column == hop.right_column == "uei"


def test_no_route_where_none_is_signed(catalog) -> None:
    assert catalog.join_paths(ISOLATED, BMF) == []


def test_a_route_never_exceeds_the_hop_bound(catalog) -> None:
    assert all(len(route) <= 1 for route in catalog.join_paths(ALL, BMF, max_hops=1))
    assert any(len(route) == 2 for route in catalog.join_paths(ALL, BMF, max_hops=2))


def test_a_relation_cannot_reach_itself(catalog) -> None:
    assert catalog.join_paths(ALL, ALL) == []


def test_the_only_reliable_identifier_edge_is_resolved(catalog) -> None:
    resolved, candidates = catalog.resolve_join(ALL, SAM)

    assert resolved is not None
    assert (resolved.left_column, resolved.right_column) == ("uei", "uei")
    assert len(candidates) == 1


def test_a_heuristic_edge_is_never_resolved_even_when_it_is_the_only_one(catalog) -> None:
    # The standing rule is that a heuristic or partial join must be reported as provisional and a
    # partial one needs context_conditions. A caller who did not ask for a fuzzy match must not get one.
    resolved, candidates = catalog.resolve_join(SAM, BMF)

    assert resolved is None
    assert len(candidates) == 1


def test_a_partial_identifier_edge_is_never_resolved(catalog) -> None:
    resolved, _ = catalog.resolve_join(ALL, TERMS)

    assert resolved is None


def test_two_paths_between_one_pair_are_not_resolved(catalog) -> None:
    add(catalog, ALL, "uei_alt", SAM, "uei_alt", "identifier", "reliable")

    resolved, candidates = catalog.resolve_join(ALL, SAM)

    assert resolved is None
    assert len(candidates) == 2


def test_an_unsigned_pair_resolves_to_nothing_rather_than_guessing(catalog) -> None:
    resolved, candidates = catalog.resolve_join(ISOLATED, ALL)

    assert resolved is None
    assert candidates == []


def test_every_returned_hop_is_a_signed_pair(catalog) -> None:
    """The trust boundary. A hop that is not in the signed catalog would be a fabricated join."""
    signed = catalog.signed_join_pairs()

    for left, right in ((ALL, SAM), (ALL, BMF), (SAM, BMF), (ALL, TERMS), (SAM, ALL)):
        for route in catalog.join_paths(left, right):
            assert route
            for hop in route:
                assert hop.as_pair() in signed or (hop.right, hop.right_column, hop.left, hop.left_column) in signed


def test_the_answer_names_the_hub_for_a_multi_dataset_question(catalog) -> None:
    result = build_path(catalog, ALL, BMF)

    assert [hop.right_source for hop in result.routes[0].hops] == [SAM, BMF]


def test_every_hop_carries_the_arguments_benthic_join_takes(catalog) -> None:
    result = build_path(catalog, ALL, SAM)

    assert result.routes[0].as_arguments() == [
        {"left_source": ALL, "left_column": "uei", "right_source": SAM, "right_column": "uei"}
    ]


def test_a_multi_hop_answer_still_carries_every_hop_arguments(catalog) -> None:
    result = build_path(catalog, ALL, BMF)

    assert result.routes[0].as_arguments() == [
        {"left_source": ALL, "left_column": "uei", "right_source": SAM, "right_column": "uei"},
        {"left_source": SAM, "left_column": "duns", "right_source": BMF, "right_column": "ein"},
    ]


def test_no_route_is_a_bounded_negative_naming_the_alternatives(catalog) -> None:
    # legislator_terms reaches bmf_organizations only in three hops, through all_entities and then
    # sam_registrations, so the bounded negative has to say where it can go instead.
    result = build_path(catalog, TERMS, BMF)

    assert result.routes == []
    assert result.note is not None
    assert "No signed route" in result.note
    assert ALL in result.note


def test_a_relation_with_no_signed_joins_says_so_plainly(catalog) -> None:
    result = build_path(catalog, ISOLATED, BMF)

    assert "No signed join touches" in str(result.note)


def test_the_same_relation_on_both_ends_is_explained_not_reported_as_missing(catalog) -> None:
    result = build_path(catalog, ALL, ALL)

    assert "same" in str(result.note)


def test_a_found_route_carries_no_negative_note(catalog) -> None:
    assert build_path(catalog, ALL, SAM).note is None


def test_a_wider_hop_bound_is_reported_so_the_bound_is_never_silent(catalog) -> None:
    result = build_path(catalog, TERMS, SAM, max_hops=3)

    assert result.max_hops == 3
