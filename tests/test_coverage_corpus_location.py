"""Where the coverage corpus lives, and nowhere else.

The coverage corpus is derived ground truth like the truth corpus, so it needs the same kind of
location contract the golden and canary suites have: one home, one name, and no case from it
anywhere else. A coverage case outside eval/coverage/ would be a second copy of the same answer
key, and a case id that does not say where it came from cannot be found when it goes stale.
"""

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_CASES = ROOT / "eval" / "coverage" / "cases.json"

# Explicit, not a glob: a glob that later matched a scratch file or a run artifact would widen this
# contract onto files nobody reviewed as ground truth.
_OTHER_SUITES: tuple[Path, ...] = (
    ROOT / "eval" / "golden" / "questions.json",
    ROOT / "eval" / "generated" / "questions.json",
    ROOT / "eval" / "canary" / "questions.json",
    ROOT / "eval" / "truth" / "numeric_cases.json",
)


def _cases(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return list(document["cases"])


def test_the_coverage_corpus_exists_at_exactly_eval_coverage_cases_json() -> None:
    assert COVERAGE_CASES.is_file(), "the coverage corpus must be committed at eval/coverage/cases.json"


def test_the_coverage_directory_holds_no_questions_json() -> None:
    # The corpus file is cases.json, never questions.json: the two questions.json suites are pinned
    # by their own contracts, and a third one here would invite a generator to write what the
    # pinned ones forbid.
    assert not (ROOT / "eval" / "coverage" / "questions.json").exists()


def test_every_coverage_case_id_starts_with_cov() -> None:
    for case in _cases(COVERAGE_CASES):
        assert case["id"].startswith("cov_"), case["id"]


@pytest.mark.parametrize("path", _OTHER_SUITES)
def test_no_coverage_case_lives_outside_eval_coverage(path: Path) -> None:
    if not path.is_file():
        pytest.skip(f"{path} is not present")
    for case in _cases(path):
        assert not case["id"].startswith("cov_"), f"{case['id']} lives in {path}, not eval/coverage/"
