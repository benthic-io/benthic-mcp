#!/usr/bin/env bash
# Stop a harness tmux session and anything it spawned.
#
#   scripts/tmux-stop.sh [session-name]
#
# The bracket in the pattern keeps pkill from matching the shell that is running this script, which
# otherwise kills the caller instead of the job.
set -euo pipefail

name="${1:-benthic-improve}"
pattern="${name//./\\.}"

tmux kill-session -t "$name" 2>/dev/null || true
pkill -f "pytho[n] .*eval/harness.py --sandbox" 2>/dev/null || true
pkill -f "pytho[n] .*eval/run_eval.py" 2>/dev/null || true

sleep 1
if tmux ls 2>/dev/null | grep -q "^${name}"; then
  echo "still running: $name"
  exit 1
fi
echo "stopped: $name"
