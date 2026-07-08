#!/bin/bash
# Launch Patchbay with crash-loop detection and known-good rollback.
# Pre-flight validate.py + rollback to .bridge-known-good.py (below) is what
# prevents a bad self-edit from bricking the bridge. On CRASH_THRESHOLD crashes
# within CRASH_WINDOW seconds we back off and surface the crash loudly; we do
# NOT auto-spawn a repair agent (that depended on the Claude CLI and fixed
# forward unsupervised — rollback already covers the bricking case). Fix forward
# by hand, or via pi from a Relay topic.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/scripts/tcc/check.sh" || true

CRASH_TIMESTAMPS="$SCRIPT_DIR/.crash-timestamps"
CRASH_WINDOW=300       # seconds — sliding window for crash counting
CRASH_THRESHOLD=3      # crashes in window before we back off and flag a loop
HEAL_BACKOFF=60        # seconds to wait after a detected crash loop before respawn
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

# Load MOP API key from pass for pydantic-ai rewrite/eval backend.
# DO NOT export as ANTHROPIC_API_KEY — that env var is inherited by every
# claude subprocess the cc-sdk/cc-sdk-mop harness spawns, which bills the
# coding turns against this API key instead of the user's Max plan
# (~$15 burned in one session before this was caught). Export under a
# scoped name; claude_sdk_mop is responsible for plumbing the key into
# pydantic-ai for eval/rewrite calls only.
if command -v pass &>/dev/null && [[ -z "$MOP_ANTHROPIC_API_KEY" ]]; then
    _mop_key="$(pass show mop-anthropic-api-key 2>/dev/null || true)"
    [[ -n "$_mop_key" ]] && export MOP_ANTHROPIC_API_KEY="$_mop_key"
fi
# Belt-and-suspenders: scrub ANTHROPIC_API_KEY from env if it leaked in
# from a parent process (shell, launchd plist, etc.).
unset ANTHROPIC_API_KEY

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

    if [[ "$recent" -ge "$CRASH_THRESHOLD" ]]; then
        # Crash loop. Known-good rollback (above) already guards against a bad
        # self-edit bricking the bridge, so we don't auto-repair here — just
        # save the crash tail for debugging, back off, and let the operator fix
        # forward. Clear timestamps so this doesn't re-trigger every respawn.
        > "$CRASH_TIMESTAMPS"
        tail -80 "$LOG" 2>/dev/null > "$SCRIPT_DIR/logs/crash-loop.log" || true
        echo "Crash loop: ${recent} crashes in ${CRASH_WINDOW}s. Tail saved to logs/crash-loop.log. Backing off ${HEAL_BACKOFF}s." >&2
        sleep "$HEAL_BACKOFF"
    else
        sleep 2
    fi
done
