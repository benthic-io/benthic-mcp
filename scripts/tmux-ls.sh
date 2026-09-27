#!/usr/bin/env bash
# List the improvement-harness sessions and show the tail of each log.
set -euo pipefail
for session in $(tmux ls -F '#{session_name}' 2>/dev/null | grep -E '^benthic-' || true); do
  log="logs/${session#benthic-}.log"
  echo "--- $session ---"
  if [ -f "$log" ]; then tail -n 8 "$log"; fi
  echo
done
