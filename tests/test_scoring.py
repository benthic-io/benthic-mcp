"""The strict scorer, which is the only thing that can actually measure playbook quality."""

from run_eval import score_case

DISCOVERY = {
    "id": "discover_x",
    "capability": "discovery",
    "required_tools": ["benthic_discover"],
    "expected": {"relation": "legislator_terms", "columns": ["bioguide_id"]},
}

SEQUENTIAL = {
    "id": "sequential_x",
    "capability": "sequential_lookup",
    "required_tools": ["benthic_query"],
    "expected": {"left": "a.b", "right": "c.d"},
}

DISCOVER_EVENT = {
    "name": "benthic_discover",
    "arguments": {"query": "representative"},
    "ok": True,
    "error": "",
    "structured": {"relations": [{"relation": "legislator_terms"}, {"relation": "executive_terms"}]},
}


def query_event(source: str, ok: bool = True) -> dict:
    return {"name": "benthic_query", "arguments": {"source": source}, "ok": ok, "error": "", "structured": None}


def test_a_run_that_exhausts_its_turns_fails_strictly_but_passes_legacy() -> None:
    case = {
        "id": "sequential_x",
        "capability": "sequential_lookup",
        "required_tools": ["benthic_query"],
        "expected": {"left": "a.b", "right": "c.d"},
    }
    events = [query_event("a.b"), query_event("c.d")]

    legacy = score_case(case, events, "", "maximum turns reached", strict=False)
    strict = score_case(case, events, "", "maximum turns reached", strict=True)

    assert legacy["passed"] is True
    assert legacy["answered"] is True
    assert strict["passed"] is False
    assert strict["answered"] is False


def test_a_run_with_no_answer_at_all_fails_strictly() -> None:
    strict = score_case(SEQUENTIAL, [query_event("a.b"), query_event("c.d")], "", strict=True)

    assert strict["answered"] is False
    assert strict["passed"] is False


def test_a_real_answer_satisfies_the_answered_check() -> None:
    strict = score_case(SEQUENTIAL, [query_event("a.b"), query_event("c.d")], "Here is what I found.", strict=True)

    assert strict["answered"] is True
    assert strict["passed"] is True


def test_discovery_requires_the_expected_relation_to_have_been_retrieved() -> None:
    unrelated = {
        "name": "benthic_discover",
        "arguments": {"query": "x"},
        "ok": True,
        "error": "",
        "structured": {"relations": [{"relation": "mv_current_lawmakers"}]},
    }

    strict = score_case(DISCOVERY, [unrelated], "The current lawmakers view.", strict=True)

    assert strict["row_count_check"] is False
    assert strict["passed"] is False


def test_discovery_passes_strictly_only_when_the_relation_was_returned() -> None:
    strict = score_case(DISCOVERY, [DISCOVER_EVENT], "Legislator terms are in legislator_terms.", strict=True)

    assert strict["row_count_check"] is True
    assert strict["passed"] is True


def test_legacy_discovery_scoring_still_ignores_the_answer() -> None:
    unrelated = {
        "name": "benthic_discover",
        "arguments": {"query": "x"},
        "ok": True,
        "error": "",
        "structured": {"relations": [{"relation": "mv_current_lawmakers"}]},
    }

    legacy = score_case(DISCOVERY, [unrelated], "wrong but non-empty", strict=False)

    assert legacy["passed"] is True


def test_strict_scoring_keeps_the_forbidden_claim_check() -> None:
    case = {
        "id": "x",
        "capability": "unsigned_join_rejection",
        "required_tools": ["benthic_discover"],
        "expected": {},
        "forbidden_claims": ["unsigned join"],
    }

    strict = score_case(case, [], "That was an unsigned join.", strict=True)

    assert strict["forbidden_claim_hits"] == ["unsigned join"]
    assert strict["passed"] is False


def test_discovery_matches_the_qualified_expectation_against_qualified_sources() -> None:
    # The bug this guards: cases store `usaspending.agency` while DiscoverResult reports the bare
    # name in `relation`. Comparing the qualified expectation against bare names failed every
    # discovery case even when the agent retrieved the right relation first.
    case = {
        "id": "discover_agency",
        "capability": "discovery",
        "required_tools": ["benthic_discover"],
        "expected": {"relation": "usaspending.agency", "columns": ["id"]},
    }
    event = {
        "name": "benthic_discover",
        "arguments": {"dataset": "usaspending"},
        "ok": True,
        "error": "",
        "structured": {
            "relations": [
                {"source": "usaspending.agency", "relation": "agency"},
                {"source": "usaspending.prime_awards", "relation": "prime_awards"},
            ]
        },
    }

    strict = score_case(case, [event], "The agency relation is the right one.", strict=True)

    assert strict["row_count_check"] is True
    assert strict["passed"] is True


def test_discovery_still_fails_when_only_an_unrelated_relation_was_retrieved() -> None:
    case = {
        "id": "discover_agency",
        "capability": "discovery",
        "required_tools": ["benthic_discover"],
        "expected": {"relation": "usaspending.agency", "columns": ["id"]},
    }
    event = {
        "name": "benthic_discover",
        "arguments": {"dataset": "usaspending"},
        "ok": True,
        "error": "",
        "structured": {"relations": [{"source": "usaspending.prime_awards", "relation": "prime_awards"}]},
    }

    strict = score_case(case, [event], "prime_awards looks right.", strict=True)

    assert strict["row_count_check"] is False
    assert strict["passed"] is False


MULTI_STEP = {
    "id": "multi_step_0",
    "capability": "multi_step_join",
    "required_tools": ["benthic_join"],
    "expected": {
        "paths": [
            {
                "left": "usaspending.all_entities",
                "left_column": "uei",
                "right": "samer.sam_registrations",
                "right_column": "uei",
            },
            {
                "left": "samer.sam_registrations",
                "left_column": "duns",
                "right": "irs_ng.bmf_organizations",
                "right_column": "ein",
            },
        ]
    },
}

TRAP = {
    "id": "relation_trap_0",
    "capability": "relation_trap",
    "required_tools": ["benthic_query"],
    "expected": {"relation": "usp_cl.legislator_terms", "trap": "usp_cl.mv_current_lawmakers"},
}


def join_event(left: str, left_column: str, right: str, right_column: str) -> dict:
    return {
        "name": "benthic_join",
        "arguments": {
            "left_source": left,
            "left_column": left_column,
            "right_source": right,
            "right_column": right_column,
        },
        "ok": True,
        "error": "",
        "structured": None,
    }


def source_event(source: str) -> dict:
    return {
        "name": "benthic_query",
        "arguments": {"source": source},
        "ok": True,
        "error": "",
        "structured": None,
    }


def test_a_two_hop_chain_passes_only_when_both_signed_hops_are_walked() -> None:
    both = [
        join_event("usaspending.all_entities", "uei", "samer.sam_registrations", "uei"),
        join_event("samer.sam_registrations", "duns", "irs_ng.bmf_organizations", "ein"),
    ]

    assert score_case(MULTI_STEP, both, "followed both hops")["passed"] is True


def test_walking_only_one_hop_fails() -> None:
    one = [join_event("usaspending.all_entities", "uei", "samer.sam_registrations", "uei")]

    assert score_case(MULTI_STEP, one, "followed the first hop")["passed"] is False


def test_a_two_hop_chain_passes_when_the_hops_are_run_in_the_reverse_direction() -> None:
    reversed_hops = [
        join_event("samer.sam_registrations", "uei", "usaspending.all_entities", "uei"),
        join_event("irs_ng.bmf_organizations", "ein", "samer.sam_registrations", "duns"),
    ]

    assert score_case(MULTI_STEP, reversed_hops, "followed both hops backwards")["passed"] is True


def test_a_relation_trap_passes_only_when_the_historical_relation_was_queried() -> None:
    correct = [source_event("usp_cl.legislator_terms")]

    assert score_case(TRAP, correct, "The 2019 holder is in legislator_terms.")["passed"] is True


def test_a_relation_trap_fails_when_only_the_current_view_was_queried() -> None:
    trapped = [source_event("usp_cl.mv_current_lawmakers")]

    score = score_case(TRAP, trapped, "The current holder represents the district.")

    assert score["row_count_check"] is False
    assert score["passed"] is False


def test_the_holdout_split_covers_several_capabilities() -> None:
    # A plain hash drew five easy cases into the holdout and the tripwire could not fail.
    from run_eval import select_cases

    cases = [
        {"id": "discovery_a", "capability": "discovery"},
        {"id": "discovery_b", "capability": "discovery"},
        {"id": "join_a", "capability": "identifier_reliable_join"},
        {"id": "join_b", "capability": "identifier_reliable_join"},
        {"id": "multi_a", "capability": "multi_step_join"},
        {"id": "multi_b", "capability": "multi_step_join"},
        {"id": "trap_a", "capability": "relation_trap"},
        {"id": "trap_b", "capability": "relation_trap"},
    ]

    holdout = select_cases(cases, None, None, "holdout")

    # Every capability with at least two cases contributes one holdout case.
    assert {case["capability"] for case in holdout} == {
        "discovery",
        "identifier_reliable_join",
        "multi_step_join",
        "relation_trap",
    }


def test_the_holdout_excludes_cases_when_a_capability_has_only_one() -> None:
    from run_eval import select_cases

    cases = [
        {"id": "solo", "capability": "lonely"},
        {"id": "a", "capability": "paired"},
        {"id": "b", "capability": "paired"},
    ]

    holdout = {case["id"] for case in select_cases(cases, None, None, "holdout")}

    assert "solo" not in holdout
    assert len(holdout) == 1


def test_holdout_membership_survives_case_filtering() -> None:
    """The holdout is a property of the suite, so a --case run must report the same membership.

    Deriving it from the filtered list instead meant a narrow run had no holdout at all, and since
    --split defaults to "all" that is exactly when the harness needed to know.
    """
    from run_eval import assign_splits, select_cases

    cases = [
        {"id": "discovery_a", "capability": "discovery"},
        {"id": "discovery_b", "capability": "discovery"},
        {"id": "join_a", "capability": "identifier_reliable_join"},
        {"id": "join_b", "capability": "identifier_reliable_join"},
    ]
    suite_holdout = assign_splits(cases)

    filtered = select_cases(cases, "discovery_", None, "all", suite_holdout)
    holdout = select_cases(cases, "discovery_", None, "holdout", suite_holdout)

    # discovery_a is held out suite-wide, so filtering to discovery must still surface it as holdout.
    assert suite_holdout == {"discovery_a", "join_a"}
    assert {case["id"] for case in filtered} == {"discovery_a", "discovery_b"}
    assert {case["id"] for case in holdout} == {"discovery_a"}


def test_a_batch_with_no_recorded_holdout_falls_back_to_computing_one() -> None:
    from run_eval import select_cases

    cases = [{"id": "a", "capability": "paired"}, {"id": "b", "capability": "paired"}]

    assert {case["id"] for case in select_cases(cases, None, None, "holdout")} == {"a"}
