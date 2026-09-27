from benthic_mcp.trace import TraceEntry, struggle_signatures


def kinds(entries: list[TraceEntry], max_turns: bool = False) -> list[str]:
    return [signature.kind for signature in struggle_signatures(entries, max_turns_hit=max_turns)]


def test_a_clean_session_produces_no_signatures() -> None:
    entries = [TraceEntry(tool="query", ok=True, row_count=5, sources=["usaspending.all_entities"])]

    assert struggle_signatures(entries) == []


def test_a_guessed_column_is_the_highest_severity_signal() -> None:
    entries = [
        TraceEntry(
            tool="query",
            ok=False,
            error="Unknown columns for usaspending.agency: agency_name",
            sources=["usaspending.agency"],
        )
    ]

    signatures = struggle_signatures(entries)

    assert [signature.kind for signature in signatures] == ["unknown_column"]
    assert signatures[0].severity == 4
    assert signatures[0].source == "usaspending.agency"


def test_an_unsigned_join_attempt_is_detected() -> None:
    entries = [
        TraceEntry(
            tool="join",
            ok=False,
            error="The requested key pair is not a signed BDP join path: a.b.c -> d.e.f",
        )
    ]

    assert "join_rejected" in kinds(entries)


def test_a_source_is_recovered_from_the_error_when_the_call_carried_none() -> None:
    entries = [
        TraceEntry(
            tool="query",
            ok=False,
            error="Unknown columns for usp_cl.legislator_terms: party_name, seat",
        )
    ]

    assert struggle_signatures(entries)[0].source == "usp_cl.legislator_terms"


def test_the_same_error_repeatedly_becomes_a_retry_loop() -> None:
    entries = [TraceEntry(tool="query", ok=False, error="boom") for _ in range(3)]

    found = struggle_signatures(entries)

    assert "retry_loop" in [signature.kind for signature in found]
    assert any("occurred 3 times" in signature.detail for signature in found)


def test_a_pair_of_identical_errors_is_only_a_repeat_not_a_retry_loop() -> None:
    entries = [TraceEntry(tool="query", ok=False, error="boom") for _ in range(2)]

    assert "repeated_error" in kinds(entries)
    assert "retry_loop" not in kinds(entries)


def test_an_incomplete_source_scan_is_flagged() -> None:
    entries = [TraceEntry(tool="query", ok=True, row_count=5, source_complete=False)]

    assert "incomplete_source" in kinds(entries)


def test_a_page_limited_result_is_not_a_struggle() -> None:
    # `truncated` with a complete source just means the agent asked for fewer rows than exist.
    # Flagging it taught the model to worry about routine pagination.
    entries = [TraceEntry(tool="query", ok=True, row_count=1, truncated=True, source_complete=True)]

    assert struggle_signatures(entries) == []


def test_an_incomplete_source_is_flagged_even_when_the_page_is_also_limited() -> None:
    entries = [TraceEntry(tool="query", ok=True, row_count=1, truncated=True, source_complete=False)]

    assert "incomplete_source" in kinds(entries)


def test_an_empty_result_alone_is_not_a_struggle() -> None:
    # Zero rows is frequently the correct answer, so flagging it on its own swamped the real signal.
    entries = [TraceEntry(tool="query", ok=True, row_count=0, sources=["samer.sam_registrations"])]

    assert struggle_signatures(entries) == []


def test_an_empty_result_followed_by_a_retry_is_a_struggle() -> None:
    entries = [
        TraceEntry(tool="query", ok=True, row_count=0, sources=["samer.sam_registrations"]),
        TraceEntry(tool="query", ok=True, row_count=4, sources=["samer.sam_registrations"]),
    ]

    signatures = struggle_signatures(entries)

    assert signatures[0].kind == "empty_then_retried"
    assert signatures[0].source == "samer.sam_registrations"


def test_exhausting_the_turns_is_a_signal_the_server_cannot_see() -> None:
    assert "max_turns" in kinds([TraceEntry(tool="query", ok=True, row_count=2)], max_turns=True)


def test_a_confidently_wrong_answer_with_clean_tool_calls_produces_nothing() -> None:
    # The documented blind spot: the server cannot see the final answer, so this case is why the
    # holdout tripwire has to exist as a backstop.
    entries = [TraceEntry(tool="query", ok=True, row_count=1)]

    assert struggle_signatures(entries) == []


def test_signatures_serialise_for_the_reflection_turn() -> None:
    signature = struggle_signatures(
        [TraceEntry(tool="query", ok=False, error="Unknown columns for a.b: c", sources=["a.b"])]
    )[0]

    payload = signature.to_json()

    assert payload["kind"] == "unknown_column"
    assert payload["source"] == "a.b"
    assert payload["severity"] == 4
