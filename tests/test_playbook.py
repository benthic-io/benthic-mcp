from typing import Literal

import pytest

from benthic_mcp.catalog import Catalog
from benthic_mcp.models import PlaybookKeyColumn, Reliability
from benthic_mcp.playbook import (
    BASE_CORE,
    DatasetSection,
    LessonRecord,
    Playbook,
    RelationGuide,
    VerifyReport,
    build_result,
    known_identifiers,
    load_playbook,
    prune_lessons,
    render_core,
    screen_prose,
    similar,
    staleness,
    verify,
)
from benthic_mcp.seed import seed_playbook
from benthic_mcp.trace import CROSS_DATASET_MERGE_THRESHOLD, MERGE_THRESHOLD, LessonStore


def test_seed_playbook_verifies_clean_against_the_signed_catalog(catalog: Catalog) -> None:
    verified, report = verify(seed_playbook(), catalog)

    assert report.total_dropped == 0, report.notes()
    assert set(verified.datasets) <= set(catalog.datasets)
    assert verified.catalog_fingerprint == catalog.fingerprint()


def test_known_identifiers_include_columns_join_keys_and_rpc_arguments(catalog: Catalog) -> None:
    known = known_identifiers(catalog)

    assert "uei" in known
    assert "ein" in known
    assert "congress" in known
    assert "radius_meters" in known
    assert "definitely_not_a_column" not in known


def test_screening_drops_a_hallucinated_relation(catalog: Catalog) -> None:
    report = VerifyReport()

    kept = screen_prose(
        "Use usaspending.prime_awards for awards. Use usaspending.nonexistent for nothing.",
        catalog,
        known_identifiers(catalog),
        report,
    )

    assert kept == ["Use usaspending.prime_awards for awards."]
    assert report.dropped_sentences == ["Use usaspending.nonexistent for nothing."]


def test_screening_drops_a_hallucinated_backticked_column(catalog: Catalog) -> None:
    report = VerifyReport()

    kept = screen_prose("Filter on `recipient_uei` first.", catalog, known_identifiers(catalog), report)
    assert kept == ["Filter on `recipient_uei` first."]

    kept = screen_prose("Filter on `recipient_ein` first.", catalog, known_identifiers(catalog), report)
    assert kept == []
    assert report.dropped_sentences == ["Filter on `recipient_ein` first."]


def test_screening_keeps_anti_patterns_that_name_a_missing_column(catalog: Catalog) -> None:
    report = VerifyReport()
    text = "There is no `org_name` column; use `org_name_current`."

    kept = screen_prose(text, catalog, known_identifiers(catalog), report)

    assert kept == [text]
    assert report.dropped_sentences == []


def test_screening_keeps_dotted_identifiers_intact(catalog: Catalog) -> None:
    report = VerifyReport()
    text = "Prefer `usaspending.prime_awards` over other award tables."

    kept = screen_prose(text, catalog, known_identifiers(catalog), report)

    assert kept == [text]
    assert report.dropped_sentences == []


def test_screening_rejects_an_unsigned_join_claim(catalog: Catalog) -> None:
    report = VerifyReport()
    text = "Join `samer.sam_registrations`.`duns` to `usaspending.all_entities`.`uei` for identity."

    kept = screen_prose(text, catalog, known_identifiers(catalog), report)

    assert kept == []
    assert report.dropped_sentences


def test_screening_keeps_a_signed_join_claim(catalog: Catalog) -> None:
    report = VerifyReport()
    text = "Join `usaspending.all_entities`.`uei` to `samer.sam_registrations`.`uei` for identity."

    assert screen_prose(text, catalog, known_identifiers(catalog), report) == [text]


def test_verify_drops_unknown_playbook_references(catalog: Catalog) -> None:
    playbook = Playbook(
        collection="ngopen",
        core=["Keep the core short."],
        relations={
            "usaspending.prime_awards": RelationGuide(preferred_columns=["award_amount", "nope"]),
            "usaspending.ghost": RelationGuide(),
        },
        datasets={
            "usp_cl": DatasetSection(
                canonical_sources=["usp_cl.legislator_terms", "usp_cl.ghost"],
                key_columns=[PlaybookKeyColumn(relation="usp_cl.legislator_terms", column="bioguide_id")],
            ),
            "ghost_dataset": DatasetSection(),
        },
    )

    verified, report = verify(playbook, catalog)

    assert "usaspending.ghost" in report.dropped_relations
    assert "usaspending.prime_awards.nope" in report.dropped_columns
    assert "usp_cl.ghost" in report.dropped_sources
    assert "ghost_dataset" in report.dropped_datasets
    assert verified.relations["usaspending.prime_awards"].preferred_columns == ["award_amount"]
    assert verified.datasets["usp_cl"].canonical_sources == ["usp_cl.legislator_terms"]


def test_only_active_lessons_are_served(catalog: Catalog) -> None:
    def lesson(lesson_id: str, status: Literal["pending", "active", "quarantined"]) -> LessonRecord:
        return LessonRecord(
            lesson_id=lesson_id,
            dataset="usp_cl",
            status=status,
            symptom=f"symptom {lesson_id}",
            lesson="Use `usp_cl.legislator_terms` with term bounds.",
        )

    verified, _ = verify(
        Playbook(
            collection="ngopen",
            lessons=[
                lesson("a", "active"),
                lesson("b", "pending"),
                lesson("c", "quarantined"),
                lesson("d", "active"),
            ],
        ),
        catalog,
    )

    assert [record.lesson_id for record in verified.lessons] == ["a", "d"]


def test_a_lesson_naming_an_unknown_relation_is_dropped(catalog: Catalog) -> None:
    playbook = Playbook(
        collection="ngopen",
        lessons=[
            LessonRecord(
                lesson_id="x",
                dataset="usp_cl",
                status="active",
                symptom="picked the wrong table",
                lesson="Use usp_cl.legislator_history instead.",
            )
        ],
    )

    _, report = verify(playbook, catalog)

    assert report.dropped_lessons == ["x"]


def test_core_slice_respects_the_token_budget(catalog: Catalog) -> None:
    tight = render_core(seed_playbook(), catalog, 50)
    generous = render_core(seed_playbook(), catalog, 2000)

    assert len(tight) < len(generous)
    assert generous.splitlines()[0].startswith("- ")
    assert all(line.startswith("- ") for line in generous.splitlines())


def test_base_core_states_the_join_safety_rules() -> None:
    text = "\n".join(BASE_CORE)

    assert "never invent a join" in text
    assert "truncated" in text


def test_staleness_is_reported_only_after_the_catalog_changes(catalog: Catalog) -> None:
    current = seed_playbook().model_copy(update={"catalog_fingerprint": catalog.fingerprint()})
    stale = seed_playbook().model_copy(update={"catalog_fingerprint": "0" * 64})

    assert staleness(current, catalog) is None
    assert "re-run" in (staleness(stale, catalog) or "")


def test_join_recipes_come_from_the_signed_catalog_not_the_playbook(catalog: Catalog) -> None:
    result = build_result(seed_playbook(), catalog, "seed", "samer", [])

    guide = result.datasets[0]
    assert guide.dataset == "samer"
    assert {(recipe.left_source, recipe.right_source) for recipe in guide.join_recipes} == {
        ("usaspending.all_entities", "samer.sam_registrations"),
        ("samer.sam_registrations", "irs_ng.bmf_organizations"),
    }
    assert all(recipe.reliability in set(Reliability) for recipe in guide.join_recipes)


def test_rpc_recipes_are_limited_to_the_datasets_that_own_them(catalog: Catalog) -> None:
    result = build_result(seed_playbook(), catalog, "seed", None, [])

    by_dataset = {guide.dataset: [recipe.operation for recipe in guide.rpc_recipes] for guide in result.datasets}
    assert by_dataset["up_cdmaps"] == ["find_district", "districts_in_bbox"]
    assert by_dataset["irs_ng"] == ["nonprofits_nearby"]
    assert by_dataset["samer"] == []


def test_build_result_rejects_an_unknown_dataset(catalog: Catalog) -> None:
    with pytest.raises(KeyError):
        build_result(seed_playbook(), catalog, "seed", "nope", [])


def test_load_playbook_reports_unavailable_for_a_missing_file(tmp_path) -> None:
    playbook, status = load_playbook(tmp_path / "absent.json")

    assert playbook is None
    assert status == "unavailable"


def test_load_playbook_reports_unavailable_for_a_corrupt_file(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    assert load_playbook(path) == (None, "unavailable")


def test_playbook_round_trips_through_json() -> None:
    original = seed_playbook()

    assert Playbook.from_json(original.to_json()) == original


def test_lesson_store_merges_repeat_reports(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons")

    def report(lesson_id: str) -> LessonRecord:
        return LessonRecord(
            lesson_id=lesson_id,
            dataset="usp_cl",
            symptom="used a current-only view for history",
            lesson="Use `usp_cl.legislator_terms`.",
        )

    store.add(report("first"))
    merged = store.merge(report("second"))

    assert len(store.all()) == 1
    assert merged.occurrences == 2
    assert merged.lesson_id == "first"


def test_lesson_store_keeps_different_datasets_apart(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons")
    symptom = "picked the wrong table"

    store.add(LessonRecord(lesson_id="a", dataset="usp_cl", symptom=symptom, lesson="one"))
    store.add(LessonRecord(lesson_id="b", dataset="usaspending", symptom=symptom, lesson="two"))

    assert len(store.all()) == 2


def test_candidate_lessons_include_every_reported_lesson_under_the_cumulative_model(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons")
    store.add(LessonRecord(lesson_id="once", symptom="a", lesson="b", confidence="medium"))
    store.add(LessonRecord(lesson_id="sure", symptom="c", lesson="d", confidence="high"))

    assert {record.lesson_id for record in store.candidate_lessons("fp")} == {"once", "sure"}


def test_enforce_cap_evicts_the_least_reinforced_active_lessons(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons")
    for index, occurrences in enumerate([1, 5, 3, 9]):
        store.add(
            LessonRecord(
                lesson_id=f"l{index}",
                symptom=f"symptom {index}",
                lesson=f"correction {index}",
                status="active",
                occurrences=occurrences,
            )
        )

    evicted = store.enforce_cap(2)

    assert set(evicted) == {"l0", "l2"}
    remaining = {record.lesson_id for record in store.by_status("active")}
    assert remaining == {"l1", "l3"}


def test_a_repeat_report_reinforces_an_active_lesson_rather_than_duplicating_it(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons")
    store.add(
        LessonRecord(
            lesson_id="served",
            status="active",
            dataset="usp_cl",
            symptom="used a current-only view for history",
            lesson="Use `usp_cl.legislator_terms`.",
            occurrences=3,
        )
    )

    merged = store.merge(
        LessonRecord(
            lesson_id="new",
            dataset="usp_cl",
            symptom="used a current-only view for history",
            lesson="Use `usp_cl.legislator_terms`.",
        )
    )

    assert len(store.all()) == 1
    assert merged.lesson_id == "served"
    assert merged.occurrences == 4
    assert store.by_status("pending") == []


def test_a_quarantined_lesson_is_never_reinforced(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons")
    store.add(
        LessonRecord(
            lesson_id="bad",
            status="quarantined",
            dataset="usp_cl",
            symptom="a bad lesson",
            lesson="do the wrong thing",
        )
    )

    store.merge(LessonRecord(lesson_id="new", dataset="usp_cl", symptom="a bad lesson", lesson="do the wrong thing"))

    assert {record.lesson_id for record in store.by_status("quarantined")} == {"bad"}
    assert [record.lesson_id for record in store.by_status("pending")] == ["new"]


def test_lesson_similarity_uses_containment_so_paraphrases_still_merge() -> None:
    # Calibrated against two real harness rounds. Containment plus content-word matching keeps
    # genuine paraphrases at 0.40 to 0.75 while unrelated lessons stay at 0.11 to 0.13, so the
    # 0.35 threshold sits inside a wide gap.
    duplicate_pairs = [
        (
            "Before finalizing, verify the source scan is complete; if it reports truncation or an "
            "incomplete scan, retrieve more",
            "When a source scan is flagged incomplete_source, do not use or propagate values from it, "
            "obtain a complete scan",
        ),
        (
            "The agent selected columns it assumed existed without confirming they exist",
            "The agent selected columns on usaspending.all_entities without confirming they exist",
        ),
    ]
    distinct = [
        "The agent reported the result as complete although the server flagged it as truncated",
        "The agent repeatedly re-fired find_district with different congress numbers after empty results",
    ]

    for left, right in duplicate_pairs:
        assert similar(left, right) >= MERGE_THRESHOLD
        for other in distinct:
            assert similar(left, other) < MERGE_THRESHOLD
            assert similar(right, other) < MERGE_THRESHOLD


def test_similarity_splits_snake_case_so_a_server_signal_name_matches_its_prose() -> None:
    # The server calls it incomplete_source; the model says "incomplete". Without splitting these
    # are unrelated tokens and a real paraphrase was missed.
    assert (
        similar(
            "The scan was flagged incomplete_source",
            "the source scan was flagged incomplete, so totals are unreliable",
        )
        >= MERGE_THRESHOLD
    )


def test_lesson_similarity_ignores_short_words_and_stop_words() -> None:
    assert similar("wrong join for the district", "wrong join for the state") > 0.3
    assert similar("", "anything") == 0.0


def test_prune_lessons_drops_anything_stale() -> None:
    from datetime import UTC, datetime, timedelta

    fresh = LessonRecord(lesson_id="fresh", symptom="a", lesson="b")
    stale = LessonRecord(
        lesson_id="stale",
        symptom="c",
        lesson="d",
        last_seen=datetime.now(UTC) - timedelta(days=90),
    )

    kept, dropped = prune_lessons([fresh, stale], 30)

    assert [record.lesson_id for record in kept] == ["fresh"]
    assert dropped == 1


def test_the_same_correction_attributed_to_two_datasets_is_stored_once(catalog: Catalog) -> None:
    # Observed in round 1: five near-identical lessons, one per dataset, because the reflector
    # scoped a method-level mistake to whichever dataset the failing case belonged to.
    store = LessonStore(tmp_path_of("cross"))
    symptom = "The agent relied on a source scan that the server flagged as incomplete and reported definitive totals"
    store.add(LessonRecord(lesson_id="first", dataset="up_cdmaps", symptom=symptom, lesson="Check source_complete."))
    merged = store.merge(
        LessonRecord(lesson_id="second", dataset="usp_cl", symptom=symptom, lesson="Check source_complete.")
    )

    assert len(store.all()) == 1
    assert merged.occurrences == 2
    # Same text under two scopes is evidence the scope was incidental.
    assert merged.dataset is None


def test_cross_dataset_matching_needs_a_higher_bar_than_same_dataset() -> None:
    assert CROSS_DATASET_MERGE_THRESHOLD > MERGE_THRESHOLD


def test_two_different_corrections_on_different_datasets_are_kept_apart() -> None:
    store = LessonStore(tmp_path_of("apart"))
    store.add(
        LessonRecord(
            lesson_id="kept",
            dataset="samer",
            symptom="the agent used the wrong district key for the join",
            lesson="Join on uei.",
        )
    )
    store.merge(
        LessonRecord(
            lesson_id="new",
            dataset="usaspending",
            symptom="the agent picked a current view for a historical question",
            lesson="Use legislator_terms with term bounds.",
        )
    )

    assert len(store.all()) == 2


def tmp_path_of(name: str):
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix=f"benthic-{name}-"))


def test_a_dataset_agnostic_lesson_is_served_for_every_dataset(catalog: Catalog) -> None:
    # An exact dataset match would silently drop every method-level lesson, which is most of them.
    verified, _ = verify(
        Playbook(
            collection="ngopen",
            lessons=[
                LessonRecord(
                    lesson_id="generic",
                    status="active",
                    dataset=None,
                    symptom="drew a conclusion from an incomplete scan",
                    lesson="Check source_complete before quoting counts.",
                ),
                LessonRecord(
                    lesson_id="scoped",
                    status="active",
                    dataset="samer",
                    symptom="joined on the wrong key",
                    lesson="Join on uei.",
                ),
            ],
        ),
        catalog,
    )

    samer = build_result(verified, catalog, "seed", "samer", []).datasets[0]
    usaspending = build_result(verified, catalog, "seed", "usaspending", []).datasets[0]

    assert len(samer.lessons) == 2
    assert len(usaspending.lessons) == 1


def test_every_seed_core_rule_actually_reaches_the_served_slice(catalog: Catalog) -> None:
    """A seed rule can be dropped from the served guidance without any error being raised.

    Two caps apply in sequence and the second one is the binding one: verify() screens the joined
    core into sentences, and render_core then takes only the first
    _MAX_CORE_LINES - len(BASE_CORE) of them. A two-sentence rule therefore lost everything past
    its first sentence, and the measured answer-delivery guidance silently stopped being served.
    """
    from benthic_mcp.playbook import render_core, verify
    from benthic_mcp.seed import SEED_CORE

    verified, _ = verify(seed_playbook(), catalog)
    served = render_core(verified, catalog, 10_000)

    for rule in SEED_CORE:
        assert rule in served, rule[:60]
