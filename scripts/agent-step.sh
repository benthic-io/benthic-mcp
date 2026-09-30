#!/usr/bin/env bash
# Hand the observer's newest findings to a headless agent session, once.
#
# Separate from tick.sh on purpose. tick.sh observes and reports; it never edits. Whether a
# failure is the server's fault or the model's is judgement, and this project has seven recorded
# cases of an unattended loop trusting a signal that was not measuring the thing asked for - a
# mutation experiment that reported 12/12 because it never loaded, a health check counting lines
# in single-line JSON, a sweep that died on ImportError and the tick printed "finished". So the
# editing is a separate, explicit step with the same gates a human turn would have.
#
# Usage: scripts/agent-step.sh [--dry-run]
#
# Requires: opencode run --auto (auto-approves permissions, or this HANGS on the first prompt).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
OBS="$ROOT/eval/observer"
STATE="${BENTHIC_AGENT_STATE:-$OBS/.agent-session}"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

if [[ "$(basename "$(git rev-parse --show-toplevel)")" == "llama.cpp" ]]; then
  echo "REFUSING: toplevel is llama.cpp, which forbids autonomous contribution."
  exit 78
fi
git diff --quiet HEAD -- src/ || { echo "REFUSING: src/ is dirty."; exit 75; }
.venv/bin/python -m pytest -m "not live" -q >/dev/null 2>&1 || { echo "REFUSING: contracts red."; exit 1; }

# One persistent session across cycles, so the agent keeps its own history and does not restart
# from nothing every 30 minutes.
if [[ ! -f "$STATE" ]]; then
  sid=$(opencode api post /api/session --data '{"title":"benthic-observe"}' 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["id"])' 2>/dev/null)
  [[ -n "$sid" ]] || { echo "could not create a session"; exit 1; }
  echo "$sid" > "$STATE"
  echo "created session $sid"
fi
SID="$(cat "$STATE")"

# Build the brief from what the observer actually verified. Only findings already checked against
# the signed manifest are included; unverified guesses are labelled as such and must not be acted on.
python3 - "$OBS" > /tmp/opencode/agent-task.md <<'PY'
import json, sys
from pathlib import Path
obs = Path(sys.argv[1])
f = json.loads((obs / "findings.json").read_text()) if (obs / "findings.json").is_file() else {}
base = obs / "baseline.json"
known = set(json.loads(base.read_text()).get("hallucinated", [])) if base.is_file() else set()
print("""One observation cycle has completed on benthic-mcp. Work the queue below, one problem at a
time, and stop after the first fix that is verified.

Rules that are not negotiable:
- Write a contract that FAILS before the fix, and prove it does. Then fix it, then prove it passes.
- Every change goes through ruff, pyright, and `pytest -m "not live"`. All must be green.
- Never add a signed join edge, an RPC allow-list entry, or anything else that widens the trust
  boundary. Stop and report if a fix seems to require one.
- Never edit src/ outside the repo. Do not push. Leave committing to a human.
- If a finding turns out to be the model's error rather than the server's, say so and change
  nothing. That outcome is common and is a real result.
- Do not chase a probe that passes on some runs and fails on others; that is variance, not a defect.

Probes that never answer in any run, and why they are not server bugs:
""")
for probe, stats in sorted(f.get("per_probe", {}).items()):
    if stats["run"] >= 3 and stats["answered"] == 0:
        print(f"  {probe}: 0/{stats['run']}")
print("\nHallucinated identifiers, each verified absent from the signed manifest:")
for item in f.get("hallucinated_identifiers", [])[:10]:
    mark = "KNOWN" if item["identifier"] in known else "NEW"
    print(f"  [{mark}] {item['kind']} {item['identifier']!r} x{item['times']}  {', '.join(item['where'][:2])}")
print("\nRecurring walls:")
for wall in f.get("recurring_walls", [])[:6]:
    print(f"  {wall['what']} x{wall['times']}")
PY

if (( DRY )); then
  echo "--- dry run: brief would be sent to session $SID ---"
  cat /tmp/opencode/agent-task.md
  exit 0
fi

echo "handing the brief to session $SID"
opencode run --session "$SID" --auto --format json < /tmp/opencode/agent-task.md \
  > "logs/agent-step-$(date +%Y%m%dT%H%M%S).jsonl" 2>&1
echo "agent step finished; output in logs/agent-step-*.jsonl"
