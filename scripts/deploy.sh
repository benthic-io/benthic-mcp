#!/usr/bin/env bash
# Deploy the current checkout to the live MCP and verify it.
#
# Two services run this code and neither reloads on its own:
#
#   llama-server.service  - owns port 8081 and spawns the MCP as a stdio child. Restarting it
#                           respawns that child, which is what picks up new code for the chat
#                           path. Restarting benthic-mcp alone does NOT: the child is a
#                           long-lived process holding the old modules.
#   benthic-mcp.service   - the HTTP service on 8082, reading ~/.config/benthic-mcp/env.
#
# `systemctl --user daemon-reload` comes first on purpose. Without it systemd reuses its cached
# command line, so an edit to a unit file followed by a restart silently runs the old flags. That
# is not hypothetical here: a `--ctx-size` and `-np` change sat in the unit file for a restart and
# came up unchanged.
#
# Every restart goes through systemd. `pkill` on the MCP binary is what this script exists to
# replace: it kills the child without letting llama-server respawn it cleanly, and it cannot
# express a rollback.
#
# Usage:
#   scripts/deploy.sh              # restart, health check, canary; roll back on red
#   scripts/deploy.sh --no-rollback # leave a red deploy in place and exit non-zero
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ROLLBACK=${BENTHIC_DEPLOY_ROLLBACK:-1}
[[ "${1:-}" == "--no-rollback" ]] && ROLLBACK=0

GOOD_SHA=""
if git diff --quiet HEAD -- src/ 2>/dev/null; then
  GOOD_SHA="$(git rev-parse HEAD)"
fi

log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { log "FAILED: $*"; exit 1; }

restart_services() {
  systemctl --user daemon-reload
  # llama-server first: it owns the stdio MCP child that the chat interface uses.
  systemctl --user restart llama-server.service || return 1
  systemctl --user restart benthic-mcp.service || return 1
  return 0
}

health_check() {
  local tries=0
  while (( tries < 30 )); do
    if curl -fsS -m 5 http://127.0.0.1:8081/health >/dev/null 2>&1 \
       && curl -fsS -m 5 http://127.0.0.1:8081/tools >/dev/null 2>&1; then
      local tools
      # `grep -c` counts matching LINES and this JSON is one line, so it answers 1 for any payload.
      tools="$(curl -fsS -m 5 http://127.0.0.1:8081/tools 2>/dev/null \
        | python3 -c 'import json,sys; print(sum(1 for t in json.load(sys.stdin) if t.get("type")=="mcp"))' 2>/dev/null)"
      [[ -n "$tools" ]] || tools=0
      (( tools >= 6 )) || { log "only ${tools} MCP tools registered"; return 1; }
      # 8082 is restarted above but was never checked here, so it could be dead or serving older
      # code while this loop went green on 8081 and called the deploy healthy. Its endpoints all
      # require the bearer token, so liveness is asked of systemd rather than over HTTP.
      [[ "$(systemctl --user is-active benthic-mcp.service)" == "active" ]] \
        || { log "benthic-mcp.service is not active after restart"; return 1; }
      log "healthy: 8081 up with ${tools} MCP tools, benthic-mcp.service active"
      return 0
    fi
    (( tries++ ))
    sleep 2
  done
  log "health check timed out"
  return 1
}

freshness_check() {
  # A restart that predates the newest edit under src/ means the process is running older code than
  # the checkout. The health check above cannot see this: it only proves the port answers. Compared
  # against file mtime rather than commit time, because a commit is recorded when it is written and
  # a deploy of correct code would otherwise read as stale.
  local newest
  newest="$(find src -name '*.py' -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)"
  [[ -n "$newest" ]] || { log "no src/ files to compare against"; return 0; }
  local unit started stale=0
  for unit in benthic-mcp.service llama-server.service; do
    started="$(date -d "$(systemctl --user show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null)" +%s 2>/dev/null || echo 0)"
    if [[ -z "$started" || "$started" == "0" ]]; then
      log "cannot read the start time of ${unit}"
      stale=1
    elif (( started < newest )); then
      log "${unit} started $(date -d @"$started" '+%Y-%m-%d %H:%M'), older than the newest src/ edit at $(date -d @"$newest" '+%Y-%m-%d %H:%M')"
      stale=1
    else
      log "${unit} is running the current src/"
    fi
  done
  return $stale
}

run_canary() {
  # ~3 minutes for six cases at 35B. `run_eval` exits non-zero when any case fails, so the exit
  # code is the verdict; the log is kept because a red deploy needs to be diagnosable.
  log "running the canary (tier 1, six cases, about three minutes)"
  local out="logs/deploy-canary.log"
  mkdir -p "$(dirname "$out")"
  if .venv/bin/python eval/run_eval.py \
        --questions eval/canary/questions.json \
        --reps 1 --strict --in-process --playbook seed \
        --max-turns 6 --no-thinking \
        --output-dir eval/validate/deploy >"$out" 2>&1; then
    log "canary green"
    return 0
  fi
  log "canary RED - see ${out}"
  return 1
}

log "restarting services (systemd only)"
restart_services || die "service restart failed"
health_check || { log "health check failed after restart"; RESTART_FAILED=1; }
freshness_check || { log "a service is running older code than the checkout"; RESTART_FAILED=1; }

if (( ${RESTART_FAILED:-0} )); then
  if (( ROLLBACK )) && [[ -n "$GOOD_SHA" ]]; then
    log "rolling back to ${GOOD_SHA:0:8}"
    git checkout --quiet "$GOOD_SHA" -- src/
    restart_services && health_check && log "rolled back to ${GOOD_SHA:0:8}"
  fi
  die "deploy did not come up healthy"
fi

if ! run_canary; then
  if (( ROLLBACK )) && [[ -n "$GOOD_SHA" ]]; then
    log "canary red after restart; rolling back to ${GOOD_SHA:0:8}"
    git checkout --quiet "$GOOD_SHA" -- src/
    if restart_services && health_check && run_canary; then
      log "ROLLBACK OK - the previous src/ is serving again"
    else
      log "ROLLBACK ALSO RED - needs a human"
    fi
  fi
  die "canary red"
fi

log "deploy OK at $(git rev-parse --short HEAD)"
