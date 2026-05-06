#!/bin/bash
# Launch Patchbay with crash-loop detection and self-healing.
# On CRASH_THRESHOLD crashes within CRASH_WINDOW seconds, fires a headless
# Claude Code session to investigate and fix. The loop keeps respawning the
# bridge regardless; CC fixes land on the next restart.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/tcc-check.sh" || true

CRASH_TIMESTAMPS="$SCRIPT_DIR/.crash-timestamps"
CRASH_WINDOW=300       # seconds — sliding window for crash counting
CRASH_THRESHOLD=3      # crashes in window before self-heal triggers
HEAL_BACKOFF=60        # seconds to wait after triggering CC before next respawn
CLAUDE_BIN="${CLAUDE_PATH:-$HOME/.local/bin/claude}"
LOG="$SCRIPT_DIR/logs/bridge.err"

# Resolve uv binary. launchd's PATH is fixed and doesn't include ~/.local/bin,
# so we can't rely on `uv` being on PATH — find it explicitly.
UV_BIN="${UV_BIN:-}"
if [[ -z "$UV_BIN" ]]; then
    for candidate in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
        if [[ -x "$candidate" ]]; then
            UV_BIN="$candidate"
            break
        fi
    done
fi
if [[ -z "$UV_BIN" ]]; then
    UV_BIN="$(command -v uv 2>/dev/null || true)"
fi
if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
    echo "FATAL: cannot locate uv. Tried \$HOME/.local/bin/uv, /opt/homebrew/bin/uv, /usr/local/bin/uv, and PATH=$PATH" >&2
    # Sleep before exiting so launchd's KeepAlive doesn't spin in a tight loop.
    sleep 30
    exit 1
fi

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

# Load MOP API key from pass for pydantic-ai rewrite backend.
# Only attempted if pass is available; MOP falls back to original text if absent.
if command -v pass &>/dev/null && [[ -z "$ANTHROPIC_API_KEY" ]]; then
    _mop_key="$(pass show mop-anthropic-api-key 2>/dev/null || true)"
    [[ -n "$_mop_key" ]] && export ANTHROPIC_API_KEY="$_mop_key"
fi

while true; do
    # Pre-flight validation — if validate.py fails, try rolling back to known-good
    if ! "$UV_BIN" run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/validate.py"; then
        echo "Validation failed. Checking for known-good backup..." >&2
        if [[ -f "$SCRIPT_DIR/.bridge-known-good.py" ]]; then
            echo "Rolling back bridge.py to .bridge-known-good.py" >&2
            cp "$SCRIPT_DIR/.bridge-known-good.py" "$SCRIPT_DIR/bridge.py"
        else
            echo "No known-good backup available. Starting bridge anyway..." >&2
        fi
    fi

    "$UV_BIN" run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/bridge.py" &
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
            -p "Patchbay (Telegram bridge) has crashed ${recent} times in ${CRASH_WINDOW}s. Investigate the crash, fix the root cause, and open a PR to the develop branch. Do NOT restart the bridge — run.sh respawns it automatically. Repo: $SCRIPT_DIR

Recent bridge.err:
${error_tail}" \
            > "$SCRIPT_DIR/logs/self-heal.log" 2>&1 &

        echo "Self-heal triggered (${recent} crashes). CC session started. Waiting ${HEAL_BACKOFF}s before next respawn." >&2
        sleep "$HEAL_BACKOFF"
    else
        sleep 2
    fi
done
