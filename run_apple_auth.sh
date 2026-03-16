#!/bin/bash
# Launch the Sign in with Apple auth server
# Cloudflare Tunnel is managed by launchd (com.cloudflare.cloudflared)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env so AUTH_PORT and other vars are available
set -a; source "$SCRIPT_DIR/.env"; set +a

echo "Starting auth server on port ${AUTH_PORT:-8443}..."
exec uv run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/auth_server.py"
