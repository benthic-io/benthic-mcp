#!/usr/bin/env bash
# Run a long job inside a detached tmux session, tee'd to a log you can tail.
#
#   scripts/tmux-run.sh <session-name> <logfile> <command> [args...]
#
# Re-running with the same session name replaces the old one, so a long improvement run can be
# restarted without hunting for stale sessions. The pane stays open after exit so the final output
# remains readable.
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: $0 <session-name> <logfile> <command> [args...]" >&2
  exit 64
fi

name="$1"
log="$2"
shift 2

workdir="$PWD"
mkdir -p "$(dirname "$log")"
log_q="$(printf '%q' "$(cd "$(dirname "$log")" && pwd)/$(basename "$log")")"
workdir_q="$(printf '%q' "$workdir")"

# Build the caller's command line as a shell-quoted string so arguments with spaces survive.
cmdline=""
for argument in "$@"; do
  cmdline+="$(printf '%q ' "$argument")"
done

tmux kill-session -t "$name" 2>/dev/null || true

runner="$(mktemp "${TMPDIR:-/tmp}/benthic-tmux-XXXXXX.sh")"
cat > "$runner" <<RUNNER
#!/usr/bin/env bash
cd $workdir_q
{
  echo "=== \$(date -Is) :: starting ==="
  $cmdline
  echo "=== finished with status \$? at \$(date -Is) ==="
} 2>&1 | tee -a $log_q
echo
echo "finished. press q to close this pane."
sleep infinity
RUNNER
chmod +x "$runner"

tmux new-session -d -s "$name" -c "$workdir" "$runner"

echo "started tmux session: $name"
echo "log:                 $log"
echo "attach:              tmux attach -t $name"
echo "follow:              tail -f $log"
echo "summary:             scripts/tmux-ls.sh"
