# Claude Telegram Bridge

Telegram bot that bridges messages to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions running on macOS. Each Telegram forum topic maps to an independent Claude session, enabling parallel conversations from mobile.

## How It Works

```
Telegram message → bridge.py → claude CLI (subprocess) → response → Telegram reply
```

The bridge spawns `claude` as a subprocess with `--output-format stream-json`, parses the structured output, and sends the result back to Telegram. Sessions persist per forum topic (or per DM chat) and auto-expire after 3 days of inactivity.

## Requirements

- macOS (uses launchd for persistent services)
- Python 3.13+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed at `/opt/homebrew/bin/claude`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```bash
# Clone and install dependencies
git clone https://github.com/TravelByRocket/claude-telegram-bridge.git
cd claude-telegram-bridge
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your bot token and Telegram user ID
```

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `ALLOWED_USER_IDS` | Yes | Comma-separated Telegram user IDs |
| `CLAUDE_PATH` | No | Path to claude CLI (default: `/opt/homebrew/bin/claude`) |
| `CLAUDE_WORKING_DIR` | No | Default working directory for Claude sessions |
| `SESSION_EXPIRY` | No | Session TTL in seconds (default: 259200 / 3 days) |
| `MAX_TIMEOUT` | No | Per-request timeout in seconds (default: 1800 / 30 min) |
| `MAX_WORKERS` | No | Concurrent Claude processes (default: 4) |

### Running

```bash
# Direct
./run.sh

# As a launchd service (auto-restart on crash)
cp com.synodic.claude-telegram-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.synodic.claude-telegram-bridge.plist
```

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Show welcome message and your user ID |
| `/new` | Start a fresh Claude session in the current topic |
| `/setproject <path>` | Set the working directory for this topic |
| `/project` | Show the current working directory |
| `/model <name>` | Switch Claude model (opus, sonnet, haiku) |
| `/kill` | Cancel a running Claude process |
| `/commitpushpr` | Commit, push, and open a PR from the current session |
| `/cleanup` | Clean up merged branches |
| `/selftest` | Run validation checks without restarting |
| `/restart` | Validate then restart the bridge |
| `/ping` | Health check |
| `/auth` | Authenticate via Sign in with Apple |
| `/lock` | Lock the session |

## Project Structure

```
bridge.py                 Main bot — long-polling event loop
validate.py               Pre-flight validation (syntax, imports, parser smoke tests)
run.sh                    Entry point with crash-loop detection and auto-rollback
auth.py                   Sign in with Apple client helpers
auth_server.py            OAuth callback server (FastAPI + Uvicorn)
run_auth.sh               Entry point for auth server + Cloudflare Tunnel
chat_projects.json        Topic → project/agent routing map
sessions/                 Per-topic session state (auto-managed)
activity.jsonl            Structured activity log
```

## Self-Edit Safety

The bridge is frequently edited by Claude Code running *through itself*. Five safety layers prevent self-edits from bricking it:

1. **`validate.py`** — Standalone validation: syntax check, import check, parser smoke tests
2. **`run.sh` pre-flight** — Runs validation before `exec python3 bridge.py`; rolls back to `.bridge-known-good.py` on failure
3. **Crash loop detection** — 3+ crashes in 5 minutes triggers auto-rollback
4. **`/restart` gate** — Validates before restarting; blocks restart on failure
5. **`ThrottleInterval: 30`** in launchd — backstop against rapid respawn

## License

Private repository. Not licensed for external use.
