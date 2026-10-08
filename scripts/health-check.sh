#!/usr/bin/env bash
# Cheap liveness check. No model, no GPU, seconds to run.
#
# This exists because the recurring failure here was a VRAM wedge, not a code change. Three wedges
# happened (2026-10-01, twice on 2026-10-04) and every one of them was fixed by a restart, so nothing
# would ever trigger a sweep in response. Polling is the only thing that catches it.
#
# Polling did not need to be the 17-probe sweep. /health answered 200 through every wedge while
# generation was dead, so the signal was never in the health endpoint alone - it was VRAM, read from
# sysfs, which costs nothing. That is why this is split out from benthic-observe.timer: the sweep is
# 25 minutes of someone's GPU per run and only measures something after a change, while this
# measures something continuously for the price of two file reads and one HTTP call.
#
# The sweep stays available on demand: scripts/tick.sh --probe
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG_DIR="${BENTHIC_HEALTH_LOG_DIR:-$ROOT/logs}"
mkdir -p "$LOG_DIR"

# Reading below this was where generation started failing. 544 MiB was free right after a restart and
# the observed creep ran about 4.2 MiB/h, so this is roughly two hours of headroom from fresh.
VRAM_FREE_WARN_MIB="${BENTHIC_VRAM_FREE_WARN_MIB:-300}"

stamp="$(date +%Y%m%dT%H%M%S)"
status=0

{
  echo "=== health $stamp ==="

  # 1. Liveness of the chat path. This alone is not sufficient - see the header - so it is one
  #    signal among several, not the check.
  if code=$(curl -fsS -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/health 2>/dev/null); then
    echo "health  8081 http $code"
  else
    echo "health  8081 UNREACHABLE"
    status=1
  fi

  # 2. VRAM. The wedge signal.
  total=$(cat /sys/class/drm/card*/device/mem_info_vram_total 2>/dev/null | head -1)
  used=$(cat /sys/class/drm/card*/device/mem_info_vram_used 2>/dev/null | head -1)
  if [[ -n "${total:-}" && -n "${used:-}" && "$total" -gt 0 ]]; then
    free=$(((total - used) / 1024 / 1024))
    echo "vram    $((used / 1024 / 1024))/$((total / 1024 / 1024)) MiB used, ${free} MiB free"
    if (( free < VRAM_FREE_WARN_MIB )); then
      echo "vram    BELOW THRESHOLD (${VRAM_FREE_WARN_MIB} MiB) - generation has wedged at this level before"
      status=1
    fi
  else
    echo "vram    unknown (no amdgpu sysfs)"
  fi

  # 3. Are the running services actually running the current source? A measurement taken against a
  #    service that predates the last commit is worse than no measurement, because it looks valid.
  src_mtime=$(find src -name '*.py' -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
  for unit in llama-server.service benthic-mcp.service; do
    # ExecMainStartTimestampMonotonic is monotonic and cannot be compared against a file mtime, so
    # the wall-clock variant is converted instead of being read straight off.
    started=$(systemctl --user show "$unit" -p ExecMainStartTimestamp --value 2>/dev/null)
    if [[ -z "$started" || "$started" == "n/a" ]]; then
      echo "service $unit: NOT RUNNING"
      status=1
      continue
    fi
    started_epoch=$(date -d "$started" +%s 2>/dev/null)
    if [[ -z "$started_epoch" ]]; then
      echo "service $unit: running (start time unparseable: $started)"
      continue
    fi
    age_min=$((( $(date +%s) - started_epoch ) / 60))
    if [[ -n "$src_mtime" && "$src_mtime" -gt "$started_epoch" ]]; then
      echo "service $unit: STALE, started $(date -d "@$started_epoch" '+%m-%d %H:%M'), src edited $(date -d "@$src_mtime" '+%m-%d %H:%M')"
      status=1
    else
      echo "service $unit: current, up ${age_min} min"
    fi
  done

  # 4. Newest observer record, and whether the server failed to answer any case in it. The sweep is
  #    on demand now, so this only changes when someone runs one - reported as unchanged rather than
  #    re-counted, so a stale count is never mistaken for a fresh one.
  newest=$(ls -1t eval/observer/records/*.jsonl 2>/dev/null | head -1)
  if [[ -n "$newest" ]]; then
    echo "record  $(basename "$newest"), $(wc -l <"$newest") cases, written $(date -d @"$(stat -c %Y "$newest")" '+%m-%d %H:%M')"
  else
    echo "record  none"
  fi

  echo "=== health $stamp exit $status ==="
} >>"$LOG_DIR/health-$stamp.log" 2>&1

exit "$status"