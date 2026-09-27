"""run_once is the seam where the two attribution arms could silently cross.

`in_process_mcp` installs the service as a module-level global, so both arms cannot share one
process. If they ever did, or if a command were built with the wrong playbook, both arms would serve
the same document and every lesson would report "no effect" - a false negative produced by a bug
rather than by the playbook. The command is therefore pinned here.
"""

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from attrib import run_once


def args() -> argparse.Namespace:
    return argparse.Namespace(
        questions="/bank/questions.json",
        llm_url="http://llm.invalid",
        model=None,
        max_turns=5,
        max_tokens=4000,
        temperature=0.6,
        top_p=0.95,
        top_k=20,
        request_timeout=180.0,
    )


class FakeRunner:
    """Stands in for subprocess.run and writes the artifact run_once reads back."""

    def __init__(self, records: list[dict], returncode: int = 0) -> None:
        self.records = records
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):  # noqa: ANN001 - mirrors subprocess.run
        self.commands.append(command)
        if self.returncode == 0:
            outdir = Path(command[command.index("--output-dir") + 1])
            run_dir = outdir / "20260101T000000Z"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "results.json").write_text(json.dumps(self.records), encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, "out", "err")


def install(monkeypatch, runner: FakeRunner) -> FakeRunner:
    monkeypatch.setattr("attrib.subprocess.run", runner)
    return runner


def test_each_arm_is_given_its_own_playbook(monkeypatch, tmp_path: Path) -> None:
    runner = install(monkeypatch, FakeRunner([{"id": "c", "score": {"passed": True}}]))
    with_arm = tmp_path / "with.json"
    without_arm = tmp_path / "without.json"

    run_once(args(), "c", with_arm, tmp_path / "a")
    run_once(args(), "c", without_arm, tmp_path / "b")

    playbooks = [command[command.index("--playbook") + 1] for command in runner.commands]
    assert playbooks == [str(with_arm), str(without_arm)]


def test_the_run_is_one_case_and_one_repetition(monkeypatch, tmp_path: Path) -> None:
    # More than one record would make the tool read whichever run directory sorted last, which may
    # belong to the other arm.
    runner = install(monkeypatch, FakeRunner([{"id": "c", "score": {"passed": True}}]))

    run_once(args(), "sequential_0_x", tmp_path / "p.json", tmp_path / "out")

    command = runner.commands[0]
    assert command[command.index("--case-filter") + 1] == "sequential_0_x"
    assert command[command.index("--reps") + 1] == "1"
    assert "--in-process" in command
    assert "--strict" in command


def test_a_failing_runner_is_reported_rather_than_read_as_a_pass(monkeypatch, tmp_path: Path) -> None:
    install(monkeypatch, FakeRunner([], returncode=1))

    with pytest.raises(SystemExit, match="exit code 1"):
        run_once(args(), "c", tmp_path / "p.json", tmp_path / "out")


def test_more_than_one_result_is_an_error_rather_than_a_silent_pick(monkeypatch, tmp_path: Path) -> None:
    install(monkeypatch, FakeRunner([{"id": "c"}, {"id": "other"}]))

    with pytest.raises(SystemExit, match="expected 1"):
        run_once(args(), "c", tmp_path / "p.json", tmp_path / "out")


def test_the_result_comes_from_the_arm_that_was_asked_for(monkeypatch, tmp_path: Path) -> None:
    install(monkeypatch, FakeRunner([{"id": "c", "score": {"passed": True}}]))

    record = run_once(args(), "c", tmp_path / "p.json", tmp_path / "out")

    assert record["id"] == "c"
