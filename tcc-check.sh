#!/bin/bash
# Check if TCC-relevant binary paths have changed since last approval.
# Called at bridge startup to warn about stale TCC permissions.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.tcc-paths"
PYTHON_BIN="$(readlink -f "$HOME/Developer/venvs/claude-telegram-bridge/bin/python3")"
NODE_BIN="$(readlink -f "$(which node 2>/dev/null)")"
CLAUDE_BIN="$(readlink -f "$(which claude 2>/dev/null)")"

CURRENT="${PYTHON_BIN}|${NODE_BIN}|${CLAUDE_BIN}"

if [ ! -f "$STATE_FILE" ]; then
    echo "$CURRENT" > "$STATE_FILE"
    echo "[tcc-check] Saved initial binary paths"
    exit 0
fi

SAVED="$(cat "$STATE_FILE")"
if [ "$CURRENT" != "$SAVED" ]; then
    echo "[tcc-check] WARNING: Binary paths changed since last TCC approval!"
    echo "[tcc-check] You will likely see macOS permission dialogs."
    echo "[tcc-check] Run: ~/Developer/venvs/claude-telegram-bridge/bin/python3 $SCRIPT_DIR/tcc-approve.py"
    echo "$CURRENT" > "$STATE_FILE"
    exit 1
fi

exit 0
