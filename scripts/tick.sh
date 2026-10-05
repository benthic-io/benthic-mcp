#!/usr/bin/env bash
# One cycle of the improvement loop: observe the live MCP, decide whether anything new is wrong,
# and stop. It does not fix anything itself.
#
# The fixing is done by an agent session, and deliberately not from here, for the reason this file
# exists to respect: deciding that a failure is the server's fault rather than the model's, or that
# a probe is unanswerable rather than merely expensive, is judgement. That judgement has been wrong
# in this project repeatedly - an instrument reported green while returning nothing, a mutation
# experiment reported 12/12 because it never loaded, and a "0 eligible" reading turned out to be a
# property of the instrument rather than the lessons. A shell loop that guesses at causes and edits
# code would manufacture exactly that class of error at scale.
#
# What this loop does safely and continuously:
#
#   - probes the live server and records every turn, including reasoning
#   - diffs the failure signatures against a baseline
#   - runs the contracts and the canary
#   - refuses to proceed if anything it depends on is red
#
# What it does not do: change code, change guidance, or deploy. It reports, and it says what is
# new. An agent takes it from there, one problem at a time.
#
# Usage: scripts/tick.sh [--probe] [--interval-minutes N]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OBS="$ROOT/eval/observer"
RECORDS="$OBS/records"
BASELINE="$OBS/baseline.json"
AUDIT="${BENTHIC_AUDIT_DIR:-/tmp/opencode/audit}/$(date +%Y%m%d)"
RUN_PROBES=0
INTERVAL=0

while (( $# )); do
  case "$1" in
    --probe) RUN_PROBES=1 ;;
    --interval-minutes) INTERVAL="$2"; shift ;;
  esac
  shift
done

mkdir -p "$RECORDS" "$AUDIT"

# One cycle at a time. A sweep takes ~25-40 minutes and the timer fires every 30, so without this
# two sweeps end up sharing two model slots and each records the other's latency as its own. That
# happened on the first unattended cycle: the 13:10 sweep was still running when the 13:47 one
# started.
LOCK="$OBS/.tick.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another tick is already running; skipping this cycle"
  exit 75
fi
LOG="$AUDIT/tick-$(date +%H%M%S).log"
say() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }

say "=== tick start (pid $$, cwd $ROOT)"

# 1. cwd guard. An unattended loop that commits must not be running inside a repository whose
#    contributing guidelines forbid autonomous agents.
if [[ "$(basename "$(git rev-parse --show-toplevel)")" == "llama.cpp" ]]; then
  say "REFUSING: toplevel is llama.cpp, which forbids autonomous contribution. Stop."
  exit 78
fi

# 2. Preconditions. Refusing to observe a broken server produces misleading findings, which is how
#    this project ended up with seven "contract violations" that were the fixtures, not the code.
if ! git diff --quiet HEAD -- src/; then
  say "REFUSING: src/ has uncommitted changes; deploy or commit before observing."
  exit 75
fi
if ! curl -fsS -m 5 http://127.0.0.1:8081/health >/dev/null 2>&1; then
  say "REFUSING: 8081 not healthy."
  exit 69
fi
MCP_TOOLS=$(curl -fsS -m 8 http://127.0.0.1:8081/tools 2>/dev/null \
  | python3 -c 'import json,sys; print(sum(1 for t in json.load(sys.stdin) if t.get("type")=="mcp"))' 2>/dev/null)
if [[ "${MCP_TOOLS:-0}" -lt 6 ]]; then
  say "REFUSING: only ${MCP_TOOLS:-0} MCP tools registered."
  exit 69
fi
say "preconditions ok (${MCP_TOOLS} tools)"

# 2b. VRAM. The recorded cause of the recurring wedge was running out of it, and it is invisible to a
#     health check: /health answered ok through every occurrence while generation was dead. Only the
#     memory number shows it coming, so it goes in the log whether or not probes run.
VRAM_TOTAL=$(cat /sys/class/drm/card*/device/mem_info_vram_total 2>/dev/null | head -1)
VRAM_USED=$(cat /sys/class/drm/card*/device/mem_info_vram_used 2>/dev/null | head -1)
if [[ -n "${VRAM_TOTAL:-}" && -n "${VRAM_USED:-}" ]]; then
  say "vram $((VRAM_USED / 1024 / 1024))/$((VRAM_TOTAL / 1024 / 1024)) MiB used"
else
  say "vram unknown (no amdgpu sysfs)"
fi

# 3. Contracts. Cheap, deterministic, no model. Red here means do not go further.
if ! .venv/bin/python -m pytest -m "not live" -q >"$AUDIT/contracts.txt" 2>&1; then
  say "CONTRACTS RED - see $AUDIT/contracts.txt"
  tail -3 "$AUDIT/contracts.txt" | tee -a "$LOG"
  exit 1
fi
say "contracts green"

# 4. Probe the live server. This is the observation.
STAMP="$(date +%Y%m%dT%H%M%S)"
if (( RUN_PROBES )); then
  say "probing (18 cases, roughly 25 minutes)"
  if ! .venv/bin/python eval/observer/sweep.py \
      --probes eval/observer/probes/core.json \
      --record "$RECORDS/${STAMP}.jsonl" \
      --max-turns 12 >>"$LOG" 2>&1; then
    say "PROBE SWEEP FAILED - see $LOG. Not writing findings."
    exit 70
  fi
  # The exit code was not the only way this lied: a sweep that writes no record still exits zero, so
  # the record itself is what has to be checked. This printed "probe sweep finished" and then built a
  # survey from records that did not exist.
  if [[ ! -s "$RECORDS/${STAMP}.jsonl" ]]; then
    say "PROBE SWEEP PRODUCED NO RECORD - expected $RECORDS/${STAMP}.jsonl. Not writing findings."
    exit 70
  fi
  say "probe sweep finished -> $RECORDS/${STAMP}.jsonl"
else
  say "probe sweep skipped (pass --probe to run one)"
fi

# 5. Survey. Reads the server's own refusals rather than the model's prose. It exits non-zero when the
#    newest cycle contains cases the server never answered, and that has to be honoured: printing
#    "survey written" regardless is how a dead server becomes a quality result.
if ! .venv/bin/python eval/observer/findings.py \
    --records "$RECORDS"/*.jsonl \
    --out "$OBS/findings.json" >>"$LOG" 2>&1; then
  say "SURVEY REFUSED - see $LOG. Not writing findings."
  exit 69
fi
say "survey written to $OBS/findings.json"

# 6. Baseline diff. Only a NEW signature is worth an agent's attention; a known one is not news,
#    and treating it as news is how a loop spends all night re-reporting the same wall.
.venv/bin/python - "$BASELINE" "$OBS/findings.json" <<'PY' | tee -a "$LOG"
import json, sys
from pathlib import Path

baseline_path, current_path = Path(sys.argv[1]), Path(sys.argv[2])
current = json.loads(current_path.read_text()) if current_path.is_file() else {}
now = sorted(i["identifier"] for i in current.get("hallucinated_identifiers", []))
walls = sorted(w["what"] for w in current.get("recurring_walls", []))

if not baseline_path.is_file():
    baseline_path.write_text(json.dumps({"hallucinated": now, "walls": walls}, indent=2) + "\n")
    print(f"  baseline created: {len(now)} identifiers, {len(walls)} walls")
    sys.exit(0)

baseline = json.loads(baseline_path.read_text())
old_ids, old_walls = set(baseline.get("hallucinated", [])), set(baseline.get("walls", []))
new_ids = [item for item in now if item not in old_ids]
new_walls = [item for item in walls if item not in old_walls]

print(f"  identifiers {len(now)} ({len(new_ids)} new), walls {len(walls)} ({len(new_walls)} new)")
for item in new_ids[:10]:
    print(f"  NEW identifier: {item}")
for item in new_walls[:6]:
    print(f"  NEW wall: {item}")
PY

say "=== tick end"
