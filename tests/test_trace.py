from datetime import UTC, datetime, timedelta

from benthic_mcp.trace import LessonStore, TraceEntry, TraceStore


def test_traces_append_to_a_jsonl_log(tmp_path) -> None:
    store = TraceStore(tmp_path / "traces", enabled=True)

    store.record(TraceEntry(tool="query", ok=True, row_count=3))
    store.record(TraceEntry(tool="join", ok=False, error_class="ToolError", error="bad column"))

    entries = store.entries()
    assert [entry.tool for entry in entries] == ["query", "join"]
    assert entries[1].error == "bad column"
    assert store.log_path.is_file()


def test_a_disabled_store_keeps_the_ring_buffer_but_writes_nothing(tmp_path) -> None:
    store = TraceStore(tmp_path / "traces", enabled=False)

    store.record(TraceEntry(tool="query", ok=True, row_count=1))

    assert not store.log_path.exists()
    assert store.recent_summary() == ["query ok rows=1"]


def test_the_ring_buffer_is_bounded(tmp_path) -> None:
    store = TraceStore(tmp_path / "traces", enabled=False, window=3)

    for index in range(10):
        store.record(TraceEntry(tool=f"t{index}", ok=True))

    assert len(store.recent()) == 3
    assert [entry.tool for entry in store.recent()] == ["t7", "t8", "t9"]


def test_recent_summary_surfaces_truncation_and_failures(tmp_path) -> None:
    store = TraceStore(tmp_path / "traces", enabled=False)

    store.record(TraceEntry(tool="query", ok=True, row_count=10, truncated=True, warning_count=2))
    store.record(TraceEntry(tool="join", ok=False, error_class="ToolError"))

    assert store.recent_summary() == [
        "query ok rows=10 truncated warnings=2",
        "join failed:ToolError",
    ]


def test_recurring_failures_only_reports_repeats(tmp_path) -> None:
    store = TraceStore(tmp_path / "traces", enabled=True)
    for _ in range(3):
        store.record(TraceEntry(tool="query", ok=False, error="Unknown columns: foo"))
    store.record(TraceEntry(tool="query", ok=False, error="one-off problem"))
    store.record(TraceEntry(tool="query", ok=True))

    assert store.recurring_failures() == [("Unknown columns: foo", 3)]


def test_sweep_drops_entries_older_than_the_retention_window(tmp_path) -> None:
    store = TraceStore(tmp_path / "traces", enabled=True, retention_days=30)
    store.directory.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    with store.log_path.open("w", encoding="utf-8") as handle:
        handle.write(f'{{"tool": "query", "ok": true, "at": "{old}"}}\n')
        handle.write(f'{{"tool": "query", "ok": true, "at": "{datetime.now(UTC).isoformat()}"}}\n')

    assert store.sweep() == 1
    assert len(store.entries()) == 1


def test_sweep_ignores_unparsable_lines(tmp_path) -> None:
    store = TraceStore(tmp_path / "traces", enabled=True, retention_days=30)
    store.directory.mkdir(parents=True, exist_ok=True)
    with store.log_path.open("w", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write("\n")

    assert store.sweep() == 1
    assert store.entries() == []


def test_trace_entry_round_trips_through_json() -> None:
    original = TraceEntry(tool="query", ok=True, row_count=7, sources=["usaspending.all_entities"])

    assert TraceEntry.from_json(original.to_json()) == original


def test_lesson_store_ignores_a_corrupt_file(tmp_path) -> None:
    store = LessonStore(tmp_path / "lessons")
    store.directory.mkdir(parents=True)
    (store.directory / "broken.json").write_text("{oops", encoding="utf-8")

    assert store.all() == []
    assert store.get("broken") is None
    assert store.set_status("broken", "active") is None


def test_lesson_store_sweep_keeps_active_records(tmp_path) -> None:
    from benthic_mcp.playbook import LessonRecord

    store = LessonStore(tmp_path / "lessons")
    old = datetime.now(UTC) - timedelta(days=90)
    store.add(LessonRecord(lesson_id="keep", symptom="a", lesson="b", status="active", last_seen=old))
    store.add(LessonRecord(lesson_id="drop", symptom="c", lesson="d", status="pending", last_seen=old))

    assert store.sweep(30) == 1
    assert [record.lesson_id for record in store.all()] == ["keep"]


def test_lesson_store_tracks_a_question_reference_without_storing_text(tmp_path) -> None:
    from benthic_mcp.playbook import LessonRecord

    store = LessonStore(tmp_path / "lessons")
    store.add(LessonRecord(lesson_id="x", symptom="a", lesson="b", question_ref="deadbeef", question_summary=None))

    record = store.get("x")
    assert record is not None
    assert record.question_ref == "deadbeef"
    assert record.question_summary is None
