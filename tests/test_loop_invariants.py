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
import tempfile
from pathlib import Path
from typing import Any

import pytest

from benthic_mcp.playbook import LessonRecord, Playbook, verify
from benthic_mcp.seed import seed_playbook
from benthic_mcp.trace import LessonStore


def tmp_path_of() -> Path:
    return Path(tempfile.mkdtemp())


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


def test_a_lesson_whose_case_no_longer_fails_is_not_rejected(tmp_path: Path) -> None:
    """ "No effect" and "nothing left to fix" must stay distinct.

    A lesson learned from a case that something else has since fixed cannot be judged there, because
    there is no failure left for it to remove. Conflating that with "no effect" throws away advice
    that has not been given a chance, which is a different failure from having no effect. The rule
    this distinction was written for was itself later removed for measuring no effect over 100
    case-runs per arm; the distinction is still right, that particular lesson was not.
    """
    from attribute_pending import GateResult, classify

    store = store_with(
        tmp_path,
        [
            lesson("stale", attribution="no_failure", status="active"),
            lesson("harmful", attribution="regresses", status="active"),
        ],
    )
    result = classify(store, GateResult([], [], [], [], [], [], []))

    assert result.untestable == ["stale"]
    assert [lesson_id for lesson_id, _ in result.rejected] == ["harmful"]
    assert fetched(store, "stale").status == "pending"


def test_an_untestable_lesson_is_not_served_and_not_quarantined(tmp_path: Path) -> None:
    from attribute_pending import GateResult, classify

    store = store_with(tmp_path, [lesson("stale", attribution="no_failure", status="active")])
    result = classify(store, GateResult([], [], [], [], [], [], []))

    assert store.candidate_lessons("fp") == []
    assert result.quarantined == []
    assert result.rejected == []


# --- re-pointing a lesson whose own case no longer reproduces -------------------------------


def test_a_lesson_is_re_measured_on_a_case_that_still_fails() -> None:
    """The one good lesson in the store was blocked forever without this.

    Its source case passes every repetition because the client stopped deliberating, so there is
    no failure there for it to remove. A substitute that does still fail gives it the chance it was
    denied.
    """
    from attribute_pending import substitute_cases

    record = lesson("stale", attribution="no_failure", attribution_case="join_now_passes")

    chosen = substitute_cases(record, ["multi_step_0_1_usaspending_irs_ng"], set(), 2)

    assert chosen == ["multi_step_0_1_usaspending_irs_ng"]


def test_a_substitute_is_never_the_holdout() -> None:
    from attribute_pending import substitute_cases

    record = lesson("stale", attribution="no_failure", attribution_case="c1")

    assert (
        substitute_cases(record, ["multi_step_0_0_usaspending_irs_ng"], {"multi_step_0_0_usaspending_irs_ng"}, 2) == []
    )


def test_a_lesson_is_never_re_measured_on_the_same_case_twice() -> None:
    # Re-running the same saturated case would reproduce the same unreadable result, at full A/B
    # cost, forever.
    from attribute_pending import substitute_cases

    record = lesson("stale", attribution="no_failure", attribution_case="c1")

    assert substitute_cases(record, ["c1", "c2"], set(), 2) == ["c2"]


def test_substitution_is_bounded_because_each_attempt_is_a_full_ab() -> None:
    from attribute_pending import substitute_cases

    record = lesson("stale", attribution="no_failure", attribution_case="c1")

    assert substitute_cases(record, ["c2", "c3", "c4", "c5"], set(), 2) == ["c2", "c3"]


def test_a_lesson_with_no_failing_case_stays_untestable() -> None:
    from attribute_pending import substitute_cases

    record = lesson("stale", attribution="no_failure", attribution_case="c1")

    assert substitute_cases(record, [], set(), 2) == []


def test_a_stalled_lesson_is_the_only_thing_asked_for_a_substitute() -> None:
    # A lesson that was never measured needs its own case, and one already measured and rejected
    # must not be re-measured every round.
    store = store_with(
        tmp_path_of(),
        [
            lesson("never", status="pending"),
            lesson("stalled", attribution="no_failure", status="pending"),
            lesson("rejected", attribution="no_effect", status="pending"),
            lesson("harmful", attribution="regresses", status="quarantined"),
        ],
    )

    assert [record.lesson_id for record in store.stalled()] == ["stalled"]


# --- suite-level attribution, the only instrument that can see a general rule ----------------


def test_a_rule_is_never_validated_against_the_holdout() -> None:
    """A rule chosen on holdout performance is a rule tuned on the answer key.

    The selection is left to the runner rather than done by writing a filtered file, because
    `assign_splits` takes one holdout case per capability and is not stable under subsetting: a
    filtered file would yield a different holdout than the rest of the harness uses.
    """
    import argparse

    from attribute_suite import arm_command

    suite = Path(__file__).resolve().parents[1] / "eval" / "generated" / "questions.json"
    args = argparse.Namespace(
        reps=1,
        strict=True,
        in_process=True,
        max_turns=5,
        llm_url="http://x",
        no_thinking=True,
        model=None,
        workdir="/tmp/x",
    )

    command = arm_command(args, "with", suite, suite)

    assert command[command.index("--split") + 1] == "tuning"


def test_the_tuning_split_of_the_suite_is_not_empty() -> None:
    from attribute_suite import tuning_split
    from run_eval import assign_splits

    suite = Path(__file__).resolve().parents[1] / "eval" / "generated" / "questions.json"
    cases = json.loads(suite.read_text(encoding="utf-8"))["cases"]

    assert len(tuning_split(suite)) == len(cases) - len(assign_splits(cases))


def test_the_two_arms_differ_by_exactly_the_rule() -> None:
    from attribute_suite import write_arm

    from benthic_mcp.playbook import load_playbook

    # Built here rather than read from eval/arms, which is gitignored: a missing base file makes
    # write_arm fall back to the seed, so the test would silently compare the seed against the seed
    # plus a rule and pass on CI while asserting nothing about the arm it was written for.
    work = Path(tempfile.mkdtemp())
    base = seed_playbook().model_copy(update={"core": ["Only a relation the manifest lists exists."]})
    base_path = work / "base.json"
    base_path.write_text(base.to_json(), encoding="utf-8")
    with_arm, without_arm = work / "with.json", work / "without.json"
    rule = "Never end the turn without a final answer."

    write_arm(with_arm, base_path, rule)
    write_arm(without_arm, base_path, None)

    with_document = load_playbook(with_arm)[0]
    without_document = load_playbook(without_arm)[0]
    assert with_document is not None and without_document is not None
    assert with_document.core == [*without_document.core, rule]


def test_a_server_that_ran_twice_as_slowly_makes_the_comparison_unreadable() -> None:
    """A wedged llama-server shows up as minutes-long runs, not as cases that flip.

    Two full runs at different moments is exactly when that happens, so the comparison says so rather
    than reporting a delta that is really a timeout.
    """
    from attribute_suite import judge

    slow = {"per_case": {}, "median_ms": 200_000}
    quick = {"per_case": {}, "median_ms": 20_000}

    assert "unreadable" in judge(quick, slow, 2)


def test_one_case_cannot_carry_a_verdict() -> None:
    from attribute_suite import judge

    base = {"per_case": {f"c{i}": [i > 0] for i in range(25)}, "median_ms": 20_000}
    one_more = {"per_case": {**base["per_case"], "c0": [True]}, "median_ms": 20_000}

    assert judge(base, one_more, 2) == "no_effect"


def test_a_rule_that_gains_two_cases_is_kept() -> None:
    from attribute_suite import judge

    base = {"per_case": {f"c{i}": [i > 2] for i in range(25)}, "median_ms": 20_000}
    better = {"per_case": {**base["per_case"], "c0": [True], "c1": [True]}, "median_ms": 20_000}

    assert judge(base, better, 2) == "fixes"


def test_every_repetition_counts_towards_the_verdict() -> None:
    """Keying by case alone silently kept only the last repetition.

    The 2-repetition run that was used to remove the answer-delivery rule read 23/25 per arm and
    "no effect" because the other repetition was discarded. Over all repetitions the same data is
    44/50 against 46/50, which crosses min_delta and reads "fixes".
    """
    from attribute_suite import _totals, judge

    without = {"per_case": {"c1": [False, False]}, "median_ms": 20_000}
    with_rule = {"per_case": {"c1": [True, True]}, "median_ms": 20_000}

    # Both repetitions: 0 -> 2, which clears min_delta.
    assert _totals(with_rule) == (2, 2)
    assert judge(without, with_rule, 2) == "fixes"
    # The bug: keeping only the last repetition gives 0 -> 1, which does not, and the run reads
    # as no effect with half the data.
    collapsed_without = {"per_case": {"c1": [False]}, "median_ms": 20_000}
    collapsed_with = {"per_case": {"c1": [True]}, "median_ms": 20_000}
    assert judge(collapsed_without, collapsed_with, 2) == "no_effect"


def test_a_case_that_passes_more_often_is_counted_as_gained() -> None:
    from attribute_suite import _moved

    gained, lost = _moved(
        {"per_case": {"a": [True, False], "b": [True, True]}},
        {"per_case": {"a": [True, True], "b": [True, False]}},
    )

    assert gained == ["a"]
    assert lost == ["b"]


# --- what may enter the always-on core once no lesson survived --------------------------------


def test_only_a_measured_rule_may_join_the_always_on_core() -> None:
    """A core line is re-sent on every turn, so it has to earn its tokens on every turn.

    Per-case attribution cannot make this call for a general rule, since a nudge can help five cases
    by one each and nothing in particular on any single one of them. So the only other admissible
    source is a suite-level measurement, and a `fixes` verdict.
    """
    import core_evidence
    from consolidate import measured_core

    class FakeCatalog:
        collections = {"ngopen": {}}

    seed_only = measured_core(FakeCatalog(), Path("/nonexistent/core-evidence.json"))
    path = Path(tempfile.mkdtemp()) / "core-evidence.json"
    core_evidence.save(
        path,
        [
            core_evidence.CoreEvidence(text="Measured rule", verdict="fixes", delta=3, reps=1),
            core_evidence.CoreEvidence(text="Measured harm", verdict="regresses", delta=-3, reps=1),
            core_evidence.CoreEvidence(text="Measured nothing", verdict="no_effect", delta=0, reps=1),
        ],
    )

    rebuilt = measured_core(FakeCatalog(), path)

    assert rebuilt == [*seed_only, "Measured rule"]


def test_a_rule_is_never_recorded_twice(tmp_path: Path) -> None:
    """One entry per rule, not per run.

    A rule that later measures as useless must not survive on the strength of an earlier pass, so a
    new measurement replaces the old one rather than accumulating beside it.
    """
    import core_evidence

    path = tmp_path / "core-evidence.json"
    report = {
        "rule": "A rule",
        "verdict": "fixes",
        "reps": 1,
        "delta": 2,
        "baseline": {"passed": 22, "cases": 25},
        "candidate": {"passed": 24, "cases": 25, "playbook": "with.json"},
    }
    core_evidence.save(path, [core_evidence.from_report(report)])
    (tmp_path / "first.json").write_text(json.dumps(report), encoding="utf-8")
    (tmp_path / "second.json").write_text(json.dumps({**report, "verdict": "regresses", "delta": -2}), encoding="utf-8")

    core_evidence.record(path, tmp_path / "first.json")
    core_evidence.record(path, tmp_path / "second.json")

    assert len(core_evidence.load(path)) == 1
    assert core_evidence.servable_lines(core_evidence.load(path)) == []


def test_a_negative_verdict_is_kept_so_the_rule_is_not_proposed_again() -> None:
    import core_evidence

    report = {
        "rule": "Tried this",
        "verdict": "no_effect",
        "reps": 1,
        "delta": 0,
        "baseline": {"passed": 24, "cases": 25},
        "candidate": {"passed": 24, "cases": 25, "playbook": "with.json"},
    }

    entry = core_evidence.from_report(report)

    assert entry.verdict == "no_effect"
    assert not entry.servable


def test_a_corrupt_evidence_file_is_not_read_as_an_empty_one(tmp_path: Path) -> None:
    """Empty means "nothing has been measured", and that would drop every rule it protects."""
    import core_evidence

    path = tmp_path / "core-evidence.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit):
        core_evidence.load(path)


def test_a_verdict_about_a_wedged_server_is_never_treated_as_a_fix(tmp_path: Path) -> None:
    import core_evidence

    path = tmp_path / "core-evidence.json"
    core_evidence.save(
        path,
        [
            core_evidence.CoreEvidence(text="A", verdict="fixes", delta=9, reps=1),
            core_evidence.CoreEvidence(
                text="B", verdict="unreadable: one arm ran more than twice as slowly", delta=9, reps=1
            ),
        ],
    )

    assert core_evidence.servable_lines(core_evidence.load(path)) == ["A"]
