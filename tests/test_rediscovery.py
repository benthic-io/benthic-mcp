"""Rediscovery is measured here, because pass rate cannot see it.

A 33-case suite read flat straight through a real regression, and the failure that motivated the
relation-pair path lookup scored 0 on its case either way: a model called benthic_discover five or
six times and never called benthic_query. Both a working session and a session spinning on discovery
produce the same pass or fail, so the cost has to be counted directly.
"""

from harness import rediscovery


def events(*names_and_sources: tuple[str, str]) -> list[dict]:
    return [{"name": name, "arguments": {"source": source}, "ok": True} for name, source in names_and_sources]


def record(events_: list[dict], case_id: str = "c", final_text: str = "an answer") -> dict:
    return {"id": case_id, "events": events_, "final_text": final_text, "score": {"passed": True}}


def test_discovery_before_any_data_use_is_counted() -> None:
    payload = record(events(("benthic_discover", "a"), ("benthic_discover", "b"), ("benthic_query", "a")))

    assert rediscovery([payload])["per_case"]["c"]["discovers_before_first_use"] == 2


def test_discovery_after_the_first_query_is_not_counted_as_rediscovery() -> None:
    payload = record(events(("benthic_query", "a"), ("benthic_discover", "b")))

    assert rediscovery([payload])["per_case"]["c"]["discovers_before_first_use"] == 0


def test_the_loop_this_matters_for_reads_as_pure_rediscovery() -> None:
    payload = record(
        events(*[("benthic_discover", "usaspending.all_entities")] * 6),
        final_text="",
    )

    report = rediscovery([payload])["per_case"]["c"]

    assert report["discovers_before_first_use"] == 6
    assert report["discovers"] == 6
    assert report["answered"] is False


def test_a_clean_two_dataset_session_reads_as_one_lookup() -> None:
    payload = record(
        events(
            ("benthic_playbook", ""),
            ("benthic_query", "usaspending.all_entities"),
            ("benthic_join", "samer.sam_registrations"),
        )
    )

    report = rediscovery([payload])["per_case"]["c"]

    assert report["discovers"] == 0
    assert report["discovers_before_first_use"] == 0
    assert report["repeated_targets"] == 0
    assert report["answered"] is True


def test_re_reading_the_same_target_is_counted_as_repeated() -> None:
    payload = record(
        events(
            ("benthic_query", "usaspending.all_entities"),
            ("benthic_query", "usaspending.all_entities"),
            ("benthic_query", "samer.sam_registrations"),
        )
    )

    assert rediscovery([payload])["per_case"]["c"]["repeated_targets"] == 1


def test_joins_and_rpcs_count_as_using_the_data() -> None:
    for tool, source in (("benthic_join", "samer.sam_registrations"), ("benthic_rpc", "up_cdmaps")):
        payload = record(events(("benthic_discover", "a"), (tool, source)))

        assert rediscovery([payload])["per_case"]["c"]["discovers_before_first_use"] == 1, tool


def test_the_means_cover_every_case_in_the_batch() -> None:
    payload = [
        record(events(("benthic_discover", "a"), ("benthic_query", "a")), case_id="clean"),
        record(events(*[("benthic_discover", "a")] * 4), case_id="loops", final_text=""),
    ]

    report = rediscovery(payload)

    assert set(report["per_case"]) == {"clean", "loops"}
    assert report["mean_calls"] == 3.0
    assert report["mean_discovers_before_first_use"] == 2.5


def test_mean_calls_when_answered_excludes_sessions_that_never_answered() -> None:
    payload = [
        record(events(("benthic_query", "a")), case_id="answered"),
        record(events(("benthic_query", "a"), ("benthic_query", "b")), case_id="silent", final_text=""),
    ]

    assert rediscovery(payload)["mean_calls_when_answered"] == 1.0


def test_a_batch_with_no_records_does_not_divide_by_zero() -> None:
    report = rediscovery([])

    assert report["mean_calls"] == 0
    assert report["mean_calls_when_answered"] is None
