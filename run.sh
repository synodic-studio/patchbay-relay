#!/bin/bash
# Launch the Claude Code Telegram bridge
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/tcc-check.sh" || true
exec "$HOME/Developer/venvs/claude-telegram-bridge/bin/python3" "$SCRIPT_DIR/bridge.py"
