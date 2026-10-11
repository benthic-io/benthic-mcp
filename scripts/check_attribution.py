"""Reject commits that carry an AI attribution trailer.

Why this is mechanical rather than a rule: commit `67615fa` reached the remote carrying
`Co-Authored-By: Claude Sonnet <noreply@anthropic.com>` and was amended 67 seconds later. The
force-push removed it from every ref, but GitHub still serves the commit, still renders the
co-author, and still counts a second contributor on the repository. `noreply@anthropic.com`
resolves to the real `claude` account, so the attribution was valid when recorded and no later push
clears it. The operator has never used Claude; an automated step wrote the trailer. A standing rule
in MEMORY.md did not prevent it, because nothing ran.

Run directly over a range:

    python scripts/check_attribution.py                 # every reachable commit
    python scripts/check_attribution.py origin/main..HEAD

Exit status is 1 when a trailer is found, so it works as a hook or a CI step.
"""

from __future__ import annotations

import re
import subprocess
import sys

# `Co-Authored-By: none` is caught deliberately: an amended rewrite that blanks the value leaves a
# trailer that means nothing but still parses as one.
TRAILER = re.compile(r"^\s*(co-authored-by|assisted-by)\s*:", re.IGNORECASE | re.MULTILINE)
GENERATED = re.compile(r"^\s*generated\s+(with|by)\b.*(claude|anthropic|chatgpt|copilot)", re.IGNORECASE | re.MULTILINE)


def scan_messages(messages: list[str]) -> list[tuple[int, str]]:
    """Return `(index, matched_line)` for each message carrying an attribution trailer."""
    found: list[tuple[int, str]] = []
    for index, message in enumerate(messages):
        for pattern in (TRAILER, GENERATED):
            match = pattern.search(message)
            if match:
                found.append((index, match.group(0).strip()))
                break
    return found


def scan_range(rev_range: str = "HEAD") -> list[tuple[str, str]]:
    """Scan commit messages in `rev_range`, newest first. Returns `(sha, offending line)`.

    `git log --format=%H%x00%B%x00` emits sha, message, NUL, sha, message, NUL..., so splitting on
    NUL yields alternating entries. Commit messages cannot contain NUL, so the split is unambiguous.
    """
    raw = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x00", rev_range],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    fields = [field for field in raw.split("\0") if field != ""]
    commits = list(zip(fields[0::2], fields[1::2]))
    findings: list[tuple[str, str]] = []
    for sha, message in commits:
        for _, line in scan_messages([message]):
            findings.append((sha, line))
    return findings


def scan_reachable_history() -> list[tuple[str, str]]:
    """Scan every commit reachable from HEAD. Empty when git or the history is unavailable."""
    if subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, check=False).returncode != 0:
        return []
    return scan_range("HEAD")


def main(argv: list[str]) -> int:
    findings = scan_range(argv[1] if len(argv) > 1 else "HEAD")
    if not findings:
        return 0
    print("AI attribution trailers are not permitted here:", file=sys.stderr)
    for sha, line in findings:
        print(f"  {sha[:9]}  {line}", file=sys.stderr)
    print(
        "\nThe operator does not use Claude and does not associate with Anthropic. A trailer on a\n"
        "pushed commit survives the force-push that removes it: GitHub keeps serving the commit and\n"
        "keeps counting the co-author. Amend and rewrite if already pushed, then ask Support to\n"
        "clear the contributor record - see MEMORY.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
