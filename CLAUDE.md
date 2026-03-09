# Stargate

Telegram bot bridge that routes messages to Claude Code sessions.

## Architecture

- `bridge.py` — Main Telegram bot, long-polling loop
- `auth.py` / `auth_server.py` — Sign in with Apple auth server
- `run.sh` — Entry point for bridge (uses `exec` to pass signals to Python)
- `run_auth.sh` — Entry point for auth server + Cloudflare Tunnel (traps SIGTERM for clean shutdown)

## Launchd Services

Both services use `KeepAlive: { SuccessfulExit: false }` so they auto-restart on crashes but stand down cleanly during macOS shutdown/restart (SIGTERM → exit 0 → no respawn).

| Plist (source of truth) | Installed to | Label |
|---|---|---|
| `com.synodic.claude-telegram-bridge.plist` | `~/Library/LaunchAgents/com.synodic.stargate.plist` | `com.synodic.stargate` |
| `dev.kj6.auth-bridge.plist` | `~/Library/LaunchAgents/` | `dev.kj6.auth-bridge` |

After editing a plist here, copy it to `~/Library/LaunchAgents/` and reload:
```bash
cp <file>.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/<file>.plist
launchctl load ~/Library/LaunchAgents/<file>.plist
```

## Python Environment

Managed with `uv`. Run `uv sync` to install dependencies. Scripts use `uv run` — no venv activation needed.
