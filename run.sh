#!/bin/bash
# Launch Stargate with crash-loop detection and self-healing.
# On CRASH_THRESHOLD crashes within CRASH_WINDOW seconds, fires a headless
# Claude Code session to investigate and fix. The loop keeps respawning the
# bridge regardless; CC fixes land on the next restart.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/tcc-check.sh" || true

CRASH_TIMESTAMPS="$SCRIPT_DIR/.crash-timestamps"
CRASH_WINDOW=300       # seconds — sliding window for crash counting
CRASH_THRESHOLD=3      # crashes in window before self-heal triggers
HEAL_BACKOFF=60        # seconds to wait after triggering CC before next respawn
CLAUDE_BIN="${CLAUDE_PATH:-/opt/homebrew/bin/claude}"
LOG="$SCRIPT_DIR/logs/bridge.err"

# Remove timestamps older than CRASH_WINDOW
prune_timestamps() {
    local cutoff
    cutoff=$(( $(date +%s) - CRASH_WINDOW ))
    if [[ -f "$CRASH_TIMESTAMPS" ]]; then
        awk -v cutoff="$cutoff" '$1 > cutoff' "$CRASH_TIMESTAMPS" > "${CRASH_TIMESTAMPS}.tmp"
        mv "${CRASH_TIMESTAMPS}.tmp" "$CRASH_TIMESTAMPS"
    fi
}

# On SIGTERM (launchd shutdown): pass signal to bridge, wait for clean exit,
# then exit 0 so launchd sees a successful exit and does not respawn.
_child_pid=""
_shutdown() {
    [[ -n "$_child_pid" ]] && kill -TERM "$_child_pid" 2>/dev/null
    wait "$_child_pid" 2>/dev/null || true
    exit 0
}
trap '_shutdown' TERM INT

while true; do
    uv run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/bridge.py" &
    _child_pid=$!
    wait "$_child_pid"
    exit_code=$?

    # Exit 0 means clean SIGTERM shutdown — stop respawning
    [[ $exit_code -eq 0 ]] && exit 0

    # Record this crash and prune stale entries
    date +%s >> "$CRASH_TIMESTAMPS"
    prune_timestamps
    recent=$(wc -l < "$CRASH_TIMESTAMPS" | tr -d ' ')

    if [[ "$recent" -ge "$CRASH_THRESHOLD" ]] && [[ -x "$CLAUDE_BIN" ]]; then
        # Clear timestamps so this doesn't re-trigger on the next crash
        > "$CRASH_TIMESTAMPS"

        error_tail=$(tail -80 "$LOG" 2>/dev/null)
        nohup "$CLAUDE_BIN" \
            --dangerously-skip-permissions \
            -p "Stargate (Telegram bridge) has crashed ${recent} times in ${CRASH_WINDOW}s. Investigate the crash, fix the root cause, and open a PR to the develop branch. Do NOT restart the bridge — run.sh respawns it automatically. Repo: $SCRIPT_DIR

Recent bridge.err:
${error_tail}" \
            > "$SCRIPT_DIR/logs/self-heal.log" 2>&1 &

        echo "Self-heal triggered (${recent} crashes). CC session started. Waiting ${HEAL_BACKOFF}s before next respawn." >&2
        sleep "$HEAL_BACKOFF"
    else
        sleep 2
    fi
done
