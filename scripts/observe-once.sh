#!/usr/bin/env bash
# One observation cycle. Driven by benthic-observe.service, on demand - the timer is disabled
# because a sweep is 25 minutes of the operator GPU and only measures something after a change.
# The 15-minute benthic-health.timer covers the continuous part (VRAM wedge) for no GPU at all.
#
# This was a `while true` loop with the timer set to OnUnitActiveSec. Those are mutually
# exclusive: the timer fires once, the service then never exits, and `OnUnitActiveSec` measures
# from the unit's last *activation*, which has not recurred - so no further elapse was ever
# scheduled and `systemctl list-timers` showed NEXT as "-". The service was still running, so it
# looked healthy while the cadence it advertised was not real.
#
# One cycle per service run, with the timer re-arming on inactivity, is the arrangement that
# actually keeps a schedule.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG_DIR="${BENTHIC_OBSERVE_LOG_DIR:-$ROOT/logs}"
mkdir -p "$LOG_DIR"
stamp="$(date +%Y%m%dT%H%M%S)"
{
  echo "=== observe cycle $stamp ==="
  # Always probe. Two model slots make an 18-case sweep affordable, and the survey is only as
  # good as the transcripts behind it.
  scripts/tick.sh --probe
  echo "=== cycle $stamp exit $? ==="
} >>"$LOG_DIR/observe-$stamp.log" 2>&1
