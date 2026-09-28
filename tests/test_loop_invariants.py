"""What has to be true for the playbook to be allowed to change itself.

A loop that edits its own instructions is only safe if the edits are gated by evidence and the
evidence is auditable. These are the invariants that make that true, written as properties rather
than as examples, because each one was a way for the system to be wrong:

- Promotion used to require only that the catalog fingerprint matched, which is a staleness check.
  Twelve grounded lessons were measured as worth no more than one hand-written rule.
- `similar` is a containment measure, so two unrelated rules sharing one function word looked like
  the same rule, and a core-line trim keyed on it dropped a legitimate line.
- A lesson with no source case could not be measured, and an unmeasurable lesson that was promoted
  anyway would be a claim nothing supports.
- `question_ref` was the hash of the empty string for a voluntary report, so a lesson with no
  provenance was indistinguishable from one that had it.
"""

import json
from pathlib import Path
from typing import Any

from benthic_mcp.playbook import LessonRecord, Playbook, verify
from benthic_mcp.seed import seed_playbook
from benthic_mcp.trace import LessonStore


def fetched(store: LessonStore, lesson_id: str) -> LessonRecord:
    record = store.get(lesson_id)
    assert record is not None, lesson_id
    return record


def store_with(tmp_path: Path, records: list[LessonRecord]) -> LessonStore:
    store = LessonStore(tmp_path / "lessons")
    for record in records:
        store.add(record)
    return store


def lesson(lesson_id: str, **overrides: Any) -> LessonRecord:
    # Grounded and distinct: catalog verification drops a lesson naming anything unknown, so a
    # placeholder string would be removed before attribution was ever consulted.
    payload: dict[str, Any] = {
        "lesson_id": lesson_id,
        "symptom": f"the query asked for a column the relation does not expose ({lesson_id})",
        "lesson": "Read the signed column list from discovery before requesting a column by name",
    }
    payload.update(overrides)
    return LessonRecord(**payload)


# --- the gate itself -----------------------------------------------------------------------


def test_nothing_is_served_without_a_measured_effect(tmp_path: Path) -> None:
    store = store_with(tmp_path, [lesson("a"), lesson("b"), lesson("c")])
    store.set_attribution("a", "fixes", case_id="c1", reps=3)
    store.set_attribution("b", "no_effect", case_id="c2", reps=3)

    assert {r.lesson_id for r in store.candidate_lessons("fp")} == {"a"}


def test_an_inconclusive_measurement_is_not_a_pass(tmp_path: Path) -> None:
    # A case that flips between repetitions cannot support "fixes", whatever the arithmetic says.
    store = store_with(tmp_path, [lesson("a")])
    store.set_attribution("a", "inconclusive", case_id="c1", reps=3)

    assert store.candidate_lessons("fp") == []


def test_a_verdict_records_the_case_and_the_rep_count_it_came_from(tmp_path: Path) -> None:
    store = store_with(tmp_path, [lesson("a")])

    store.set_attribution("a", "fixes", case_id="join_1", reps=5)
    record = fetched(store, "a")

    assert record is not None
    assert record.attribution_case == "join_1"
    assert record.attribution_reps == 5
    assert record.attributed_at is not None


# --- inheriting a verdict, which is what keeps the gate affordable -------------------------


def test_a_rephrasing_of_a_measured_lesson_inherits_its_verdict(tmp_path: Path) -> None:
    # The reflector paraphrases, so identical advice arrives as a new record every round. Measuring
    # each copy would cost an A/B per lesson per round forever.
    text = "confirm the real column names from the schema before requesting them"
    store = store_with(tmp_path, [lesson("donor", lesson=text)])
    store.set_attribution("donor", "no_effect", case_id="c1", reps=3)
    store.add(lesson("copy", lesson=f"Before you request a column, {text}."))

    donor = store.inheritable(fetched(store, "copy"))

    assert donor is not None
    assert donor.lesson_id == "donor"


def test_inherited_evidence_names_the_lesson_that_gathered_it(tmp_path: Path) -> None:
    text = "confirm the real column names from the schema before requesting them"
    store = store_with(tmp_path, [lesson("donor", lesson=text)])
    store.set_attribution("donor", "fixes", case_id="c1", reps=3)
    store.add(lesson("copy", lesson=text))

    donor = store.inheritable(fetched(store, "copy"))
    assert donor is not None
    store.set_attribution("copy", donor.attribution, case_id=donor.attribution_case, inherited_from=donor.lesson_id)
    copy = fetched(store, "copy")

    assert copy is not None
    assert copy.attribution_inherited_from == "donor"
    assert copy.attribution == "fixes"


def test_an_unrelated_lesson_inherits_nothing(tmp_path: Path) -> None:
    # The failure this guards: containment similarity matched two unrelated rules on a shared
    # function word, so a lesson could inherit a verdict that says nothing about it.
    store = store_with(
        tmp_path,
        [lesson("donor", lesson="Never use a current-only view to answer a historical question")],
    )
    store.set_attribution("donor", "no_effect", case_id="c1", reps=3)
    store.add(
        lesson(
            "other",
            lesson="Paginate until every page of a truncated scan is retrieved",
            symptom="counts came back short because the source was truncated",
        )
    )

    assert store.inheritable(fetched(store, "other")) is None


def test_a_lesson_never_inherits_from_itself(tmp_path: Path) -> None:
    store = store_with(tmp_path, [lesson("a", lesson="check the column names from the schema")])
    store.set_attribution("a", "fixes", case_id="c1", reps=3)

    assert store.inheritable(fetched(store, "a")) is None


# --- provenance: a lesson nobody can trace cannot be served --------------------------------


def test_a_lesson_with_no_source_case_cannot_be_attributed(tmp_path: Path) -> None:
    from attribute_pending import source_case

    assert source_case(lesson("a")) is None
    assert source_case(lesson("b", source_ref="join_1")) == "join_1"


def test_an_untestable_lesson_stays_pending_and_therefore_unserved(tmp_path: Path) -> None:
    store = store_with(tmp_path, [lesson("a")])

    assert store.get("a") is not None
    assert store.candidate_lessons("fp") == []
    assert [r.lesson_id for r in store.untested()] == ["a"]


# --- what the served document may contain ---------------------------------------------------


def test_every_served_lesson_carries_its_evidence(catalog) -> None:
    """The audit property: anything in the document can be traced to a measurement."""
    store = store_with(Path("/tmp/does-not-matter"), [lesson("a", status="active")])
    store.set_attribution("a", "fixes", case_id="c1", reps=3)
    document = seed_playbook().model_copy(update={"lessons": [fetched(store, "a")]})

    verified, _ = verify(document, catalog)

    for record in verified.lessons:
        assert record.attribution == "fixes"
        assert record.attribution_case
        assert record.attribution_reps > 0


def test_verification_does_not_invent_evidence(catalog) -> None:
    # verify keeps only lessons the store has already marked active, so this has to be active to
    # survive at all - and it must come out still untested, not promoted by being in the document.
    document = seed_playbook().model_copy(update={"lessons": [lesson("a", status="active")]})

    verified, _ = verify(document, catalog)

    assert [record.attribution for record in verified.lessons] == ["untested"]


def test_a_pending_lesson_is_never_served_even_when_it_is_in_the_document(catalog) -> None:
    document = seed_playbook().model_copy(update={"lessons": [lesson("a")]})

    verified, _ = verify(document, catalog)

    assert verified.lessons == []


def test_a_lesson_the_catalog_rejects_is_dropped_even_with_evidence(catalog) -> None:
    store = store_with(Path("/tmp/does-not-matter"), [lesson("a", status="active", relation="usaspending.nope")])
    store.set_attribution("a", "fixes", case_id="c1", reps=3)
    document = seed_playbook().model_copy(update={"lessons": [fetched(store, "a")]})

    verified, report = verify(document, catalog)

    assert verified.lessons == []
    assert report.dropped_lessons == ["a"]


# --- the store cannot grow without bound, and cannot grow on repetition alone ---------------


def test_reinforcement_does_not_substitute_for_evidence(tmp_path: Path) -> None:
    store = store_with(tmp_path, [lesson("a", occurrences=9)])

    assert store.candidate_lessons("fp") == []


def test_the_cap_still_applies_to_active_lessons(tmp_path: Path) -> None:
    store = store_with(tmp_path, [lesson(f"l{i}", occurrences=i) for i in range(1, 6)])
    for index in range(1, 6):
        store.set_status(f"l{index}", "active")

    evicted = store.enforce_cap(2)

    assert len(evicted) == 3
    assert {r.lesson_id for r in store.by_status("active")} == {"l5", "l4"}


def test_a_quarantined_lesson_is_not_a_candidate(tmp_path: Path) -> None:
    store = store_with(tmp_path, [lesson("a")])
    store.set_attribution("a", "fixes", case_id="c1", reps=3)
    store.set_status("a", "quarantined")

    assert store.candidate_lessons("fp") == []


def test_the_document_survives_a_json_round_trip_with_its_evidence(tmp_path: Path) -> None:
    store = store_with(tmp_path, [lesson("a")])
    store.set_attribution("a", "fixes", case_id="c1", reps=4)
    record = fetched(store, "a")
    assert record is not None

    reloaded = Playbook.from_json(
        json.dumps(
            Playbook(collection="ngopen", lessons=[record]).to_json()
            and Playbook(collection="ngopen", lessons=[record]).model_dump(mode="json")
        )
    )

    assert reloaded.lessons[0].attribution == "fixes"
    assert reloaded.lessons[0].attribution_reps == 4
    assert reloaded.lessons[0].attributed_at is not None


def test_a_lesson_from_the_holdout_is_never_measured_against_it() -> None:
    """Measuring there would be training on the holdout through the back door.

    Three of the twelve accumulated lessons were learned from holdout cases, before the harness
    stopped reflecting on them. The gate has to notice, or it will measure them and trust the result.
    """
    from attribute_pending import holdout_cases

    holdout = holdout_cases(str(Path(__file__).resolve().parents[1] / "eval" / "generated" / "questions.json"))

    assert holdout, "the suite must actually have a holdout for this to mean anything"
    assert "multi_step_0_0_usaspending_irs_ng" in holdout
    assert "multi_step_0_1_usaspending_irs_ng" not in holdout


BASELINE_LESSONS = [
    LessonRecord(lesson_id="evidenced", symptom="s", lesson="l", status="active", attribution="fixes"),
    LessonRecord(lesson_id="legacy", symptom="s2", lesson="l2", status="active"),
    LessonRecord(lesson_id="disproven", symptom="s3", lesson="l3", status="active", attribution="no_effect"),
]


def test_a_lesson_with_no_evidence_is_never_inherited_by_the_next_document(tmp_path: Path) -> None:
    """The document was self-perpetuating.

    Consolidation carried every lesson in the current document forward, so a lesson put there by a
    round that no longer applies stayed there regardless of the gate and no measurement could ever
    reach it. The carry-forward set is now filtered by the same rule as new promotion.
    """
    from consolidate import carry_forward

    store = store_with(tmp_path, BASELINE_LESSONS)

    carried, demoted = carry_forward(BASELINE_LESSONS, store)

    assert [record.lesson_id for record in carried] == ["evidenced"]
    assert sorted(demoted) == ["disproven", "legacy"]


def test_a_demoted_lesson_becomes_pending_so_the_gate_can_measure_it(tmp_path: Path) -> None:
    from consolidate import carry_forward

    store = store_with(tmp_path, BASELINE_LESSONS)
    store.set_status("legacy", "active")

    carry_forward(BASELINE_LESSONS, store)
    legacy = fetched(store, "legacy")

    assert legacy.status == "pending"
    # Only the never-measured one is re-queued. A lesson already measured and rejected is not
    # silently re-tested every round.
    assert [record.lesson_id for record in store.untested()] == ["legacy"]


def test_an_evidenced_lesson_keeps_its_status_across_a_round(tmp_path: Path) -> None:
    from consolidate import carry_forward

    store = store_with(tmp_path, BASELINE_LESSONS)
    store.set_status("evidenced", "active")

    carry_forward(BASELINE_LESSONS, store)

    assert fetched(store, "evidenced").status == "active"
