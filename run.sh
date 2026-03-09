#!/bin/bash
# Launch Stargate
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/tcc-check.sh" || true
exec uv run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/bridge.py"
