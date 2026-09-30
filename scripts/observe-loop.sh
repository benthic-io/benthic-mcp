#!/usr/bin/env bash
# Run tick.sh on a schedule, unattended.
#
# Deliberately a loop of the *observation* cycle only. It probes, surveys, and reports; it never
# edits code, guidance, or the served playbook. The fixing is judgement work and is done by an
# agent session, one problem at a time, with a contract that fails without its fix.
#
# This is the whole unattended scope, and it is the part that should not need a human:
#
#   - every use of the MCP is recorded, including the model's reasoning
#   - every refusal is attributed to an identifier and checked against the signed manifest
#   - a new identifier or a new recurring wall is named, once, rather than every ten minutes
#
# What it will not do unattended: change code, change guidance, or deploy. A loop that guesses
# at causes and edits would manufacture exactly the false-green failures this project spent the
# session learning to distrust.
#
# Install:
#   systemctl --user enable --now benthic-observe.timer
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG_DIR="${BENTHIC_OBSERVE_LOG_DIR:-$ROOT/logs}"
mkdir -p "$LOG_DIR"

echo "starting at $(date -Is), every ${BENTHIC_OBSERVE_INTERVAL:-30} minutes" | tee -a "$LOG_DIR/observe.log"

while true; do
  stamp="$(date +%Y%m%dT%H%M%S)"
  {
    echo "=== observe cycle $stamp ==="
    # A probe sweep every cycle; it is the only thing that sees how the model actually uses the
    # tools, and two slots on the model make it affordable.
    if ! scripts/tick.sh --probe; then
      echo "cycle exited non-zero at $stamp (see the tick log)"
    fi
  } >>"$LOG_DIR/observe-$stamp.log" 2>&1
  sleep "${BENTHIC_OBSERVE_INTERVAL:-30}"
done
