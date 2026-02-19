#!/bin/bash
# Launch the Sign in with Apple auth server + Cloudflare Tunnel
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cleanup() {
    echo "Shutting down..."
    kill $AUTH_PID $TUNNEL_PID 2>/dev/null
    wait $AUTH_PID $TUNNEL_PID 2>/dev/null
    exit 0
}
trap cleanup INT TERM

# Start auth server
echo "Starting auth server on port 8443..."
"$HOME/Developer/venvs/claude-telegram-bridge/bin/python3" "$SCRIPT_DIR/auth_server.py" &
AUTH_PID=$!

# Start Cloudflare Tunnel
echo "Starting Cloudflare Tunnel for auth.kj6.dev..."
cloudflared tunnel run auth-bridge &
TUNNEL_PID=$!

echo "Auth server PID=$AUTH_PID, Tunnel PID=$TUNNEL_PID"
echo "Press Ctrl+C to stop both"

wait
