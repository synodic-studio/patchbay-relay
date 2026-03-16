# Stargate

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
git clone <your-repo-url>
cd stargate
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your bot token and Telegram user ID
```

See `.env.example` for the full list of environment variables (bridge, auth, Apple Sign In).

### Running

```bash
# Direct
./run.sh

# As a launchd service (auto-restart on crash)
cp com.synodic.claude-telegram-bridge.plist ~/Library/LaunchAgents/com.synodic.stargate.plist
launchctl load ~/Library/LaunchAgents/com.synodic.stargate.plist
```

## Project Structure

```
bridge.py                 Entrypoint — Telegram handlers, command handlers, lifecycle
stargate/                 Core package (config, sessions, parser, quota, activity, projects)
validate.py               Pre-flight validation (syntax, imports, parser smoke tests)
run.sh                    Entry point with crash-loop detection, validation, and self-healing
auth.py                   Sign in with Apple client helpers
auth_server.py            OAuth callback server (FastAPI + Uvicorn)
run_auth.sh               Entry point for auth server + Cloudflare Tunnel
chat_projects.json        Topic → project/agent routing map (gitignored)
tests/                    Test suite (pytest)
sessions/                 Per-topic session state (auto-managed, gitignored)
activity.jsonl            Structured activity log (gitignored)
```

## Self-Edit Safety

The bridge is frequently edited by Claude Code running *through itself*. Five safety layers prevent self-edits from bricking it:

1. **`validate.py`** — Standalone validation: syntax check, import check, parser smoke tests
2. **`run.sh` pre-flight** — Runs `validate.py` before starting `bridge.py`; rolls back to `.bridge-known-good.py` on failure
3. **Crash loop detection** — 3+ crashes in 5 minutes triggers a Claude Code self-heal session
4. **`/restart` gate** — Validates before restarting; blocks restart on failure
5. **`ThrottleInterval: 30`** in launchd — backstop against rapid respawn

## Development

```bash
uv run pytest tests/ -q                     # run tests
uv run ruff check .                          # lint
uv run python validate.py                    # pre-flight smoke tests
uv run pytest tests/ --cov --cov-report=term-missing  # with coverage
```

## License

Private repository. Not licensed for external use.
