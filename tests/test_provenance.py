"""A lesson has to say where it came from, or it cannot be attributed to anything.

`question_ref` is a hash of the question that produced the lesson, which is what lets a later run
ask whether that lesson fixed that case. A voluntary `benthic_report` need not carry a question, and
hashing the empty string produced a plausible-looking reference that resolved to nothing, so a
lesson with no traceable source was indistinguishable from one that had a source.
"""

import hashlib
from typing import Any

import pytest

from benthic_mcp.service import BenthicService

pytestmark = pytest.mark.asyncio


def runtime(catalog) -> Any:
    class Runtime:
        def __init__(self) -> None:
            self.catalog = catalog
            self.playbook = None
            self.result = None

    return Runtime()


async def service_for(catalog, settings) -> BenthicService:
    service = BenthicService(settings)
    service.playbook = lambda: _completed(runtime(catalog))  # type: ignore[method-assign]
    return service


async def _completed(value: Any) -> Any:
    return value


async def test_a_report_with_no_question_records_no_reference(catalog, settings) -> None:
    service = await service_for(catalog, settings)
    try:
        report = await service.record_lesson(symptom="guessed a column", lesson="check the schema")
        record = service.lesson_store.get(report.lesson_id)

        assert record is not None
        assert record.question_ref == ""
    finally:
        await service.close()


async def test_a_report_with_a_question_records_a_resolvable_reference(catalog, settings) -> None:
    service = await service_for(catalog, settings)
    try:
        report = await service.record_lesson(
            symptom="guessed a column",
            lesson="check the schema",
            question_summary="which district did the CA representative run in during 2019",
        )
        record = service.lesson_store.get(report.lesson_id)

        assert record is not None
        assert (
            record.question_ref
            == hashlib.sha256(b"which district did the CA representative run in during 2019").hexdigest()[:16]
        )
    finally:
        await service.close()


async def test_an_absent_reference_is_distinguishable_from_a_present_one(catalog, settings) -> None:
    service = await service_for(catalog, settings)
    try:
        without = await service.record_lesson(symptom="a", lesson="b")
        with_question = await service.record_lesson(symptom="c", lesson="d", question_summary="a question")

        absent = service.lesson_store.get(without.lesson_id)
        present = service.lesson_store.get(with_question.lesson_id)

        assert absent is not None and present is not None
        assert absent.question_ref != present.question_ref
    finally:
        await service.close()
