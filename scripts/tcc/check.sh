#!/bin/bash
# Check if TCC-relevant binary paths have changed since last approval.
# Intended for use at host startup to warn about stale TCC permissions.
#
# Resolves the realpath of the current `python3`, `node`, and `claude`
# on PATH and saves them to .tcc-paths next to this script. On every
# subsequent run, compares against the saved state and prints a warning
# (exit 1) if anything changed — that's when macOS silently revokes TCC.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.tcc-paths"

PYTHON_BIN="$(readlink -f "$(command -v python3 2>/dev/null)" 2>/dev/null || true)"
NODE_BIN="$(readlink -f "$(command -v node 2>/dev/null)" 2>/dev/null || true)"
CLAUDE_BIN="$(readlink -f "$(command -v claude 2>/dev/null)" 2>/dev/null || true)"

CURRENT="${PYTHON_BIN}|${NODE_BIN}|${CLAUDE_BIN}"

if [ ! -f "$STATE_FILE" ]; then
    echo "$CURRENT" > "$STATE_FILE"
    echo "[tcc-check] Saved initial binary paths"
    exit 0
fi

SAVED="$(cat "$STATE_FILE")"
if [ "$CURRENT" != "$SAVED" ]; then
    echo "[tcc-check] WARNING: Binary paths changed since last TCC approval!"
    echo "[tcc-check] macOS may have silently revoked permissions."
    echo "[tcc-check] Re-approve with: <your-python> $SCRIPT_DIR/approve.py"
    echo "$CURRENT" > "$STATE_FILE"
    exit 1
fi

exit 0
