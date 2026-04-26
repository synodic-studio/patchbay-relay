# Stargate

**Run AI coding agents on your computer, from your phone.**

Stargate bridges Telegram to AI coding agents running on your own machine — Claude Code, the Claude Agent SDK, Aider, OpenCode, and more — letting you develop software, manage infrastructure, and run autonomous agents from a mobile messaging app while your real workstation does the actual work.

```
Telegram message --> bridge.py --> agent harness (subprocess) --> response --> Telegram reply
```

Each Telegram forum topic maps to an independent agent session. Multiple topics run in parallel, each with its own project directory, harness backend, and session state.

## Features

- **Session Management** -- Create, switch, and resume Claude Code sessions per Telegram topic. Sessions persist across messages and auto-expire after configurable inactivity.
- **Multi-Project Routing** -- Route Telegram topics to different project directories via `chat_projects.json`. Each topic can target a different codebase.
- **Agent Identity Loading** -- Per-topic agent configuration injects identity files (SOUL.md, IDENTITY.md, AGENTS.md) into Claude's system prompt, enabling specialized agent personas.
- **Sign in with Apple** -- Optional OIDC authentication via Apple's identity service with IP pinning, absolute expiry, and inactivity timeout.
- **TOTP Authentication** -- Alternative auth method using standard authenticator apps (Google Authenticator, Authy, 1Password).
- **Crash Recovery** -- Self-healing crash loop detection: 3+ crashes in 5 minutes triggers an autonomous Claude Code session to diagnose and fix the issue.
- **Pre-flight Validation** -- `validate.py` checks syntax, imports, and parser behavior before every bridge start. On failure, rolls back to a known-good snapshot.
- **Quota Handoff** -- When Claude hits rate limits, the task is written to a queue file for background processing.
- **Rich Messaging** -- Markdown rendering, code blocks, photo/image handling with captions.
- **Pending Message Batching** -- Messages arriving while Claude is processing are queued and batched into a single follow-up invocation.
- **Remote Control** -- Start `claude remote-control` sessions from Telegram for direct CLI access.
- **Test Suite** -- 420 tests across 16 test files with comprehensive coverage of all `stargate/` modules.

## Architecture

```
bridge.py                 Entrypoint -- Telegram handlers, commands, lifecycle
stargate/                 Core package
  config.py               Environment variables, paths, constants, logging
  sessions.py             Session persistence, sanitization, pending messages
  parser.py               Claude CLI output parsing (JSON array, NDJSON, single-object)
  quota.py                Quota/rate-limit detection and queue handoff
  activity.py             Structured JSON-lines activity logging
  projects.py             Chat-to-project directory mapping
auth.py                   Authentication state management (Apple Sign In + TOTP)
auth_server.py            FastAPI server for Sign in with Apple OIDC flow
validate.py               Pre-flight validation (syntax, imports, parser smoke tests)
run.sh                    Entry point with crash-loop detection and self-healing
run_auth.sh               Entry point for auth server + Cloudflare Tunnel
```

## Prerequisites

- macOS (uses launchd for process management)
- Python 3.13+
- [uv](https://docs.astral.sh/uv/) for dependency management
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Apple Developer account (only if using Sign in with Apple auth)

## Setup

```bash
# Clone and install dependencies
git clone https://github.com/synodic-studio/stargate.git
cd stargate
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your bot token and allowed Telegram user IDs
```

See `.env.example` for all configuration options including auth server settings.

### Running Directly

```bash
./run.sh
```

### Running as a launchd Service

The included plist files provide persistent services that auto-restart on crash. Update the paths in each plist to match your system (look for `YOURUSER` placeholders):

```bash
# Bridge service
cp com.synodic.claude-telegram-bridge.plist ~/Library/LaunchAgents/com.synodic.stargate.plist
launchctl load ~/Library/LaunchAgents/com.synodic.stargate.plist

# Auth server + Cloudflare Tunnel (only if using Sign in with Apple)
cp dev.kj6.auth-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/dev.kj6.auth-bridge.plist
```

### Authentication (Optional)

Set `AUTH_REQUIRED=true` in `.env` to enforce authentication. Without it, all messages from `ALLOWED_USER_IDS` pass through.

**Sign in with Apple** requires an Apple Developer account with a Services ID, private key, and registered redirect URL. Configure the `APPLE_*` variables in `.env`.

**TOTP** can be set up per user:
```bash
uv run setup_totp.py --user-id <telegram-user-id>
```

## Telegram Commands

| Command | Auth | Description |
|---|---|---|
| `/start` | No | Show command list and your Telegram user ID |
| `/auth` | No | Send auth link or show current session info |
| `/lock` | Yes | Lock your session (`/lock all` locks all sessions) |
| `/clearnew` | No | Start a fresh session (conversation history lost) |
| `/setproject` | No | Bind topic to a project directory |
| `/project` | No | Show current project and agent for this topic |
| `/kill` | Yes | Kill the active Claude subprocess (session preserved) |
| `/restart` | Yes | Restart the bridge process |
| `/remote_control` | Yes | Start/stop a `claude remote-control` session |
| `/ping` | No | Liveness check with active session status |

## Self-Edit Safety

The bridge is frequently edited by Claude Code running *through itself*. Five safety layers prevent self-edits from bricking it:

1. **`validate.py`** -- Standalone validation: syntax check, import check, parser smoke tests
2. **`run.sh` pre-flight** -- Runs `validate.py` before starting; rolls back to known-good on failure
3. **Crash loop detection** -- 3+ crashes in 5 minutes triggers an autonomous Claude Code self-heal session
4. **`/restart` gate** -- Validates before restarting; blocks restart on failure
5. **`ThrottleInterval: 30`** in launchd -- backstop against rapid respawn

## Development

```bash
uv run pytest tests/ -q                     # run tests
uv run ruff check .                          # lint
uv run python validate.py                    # pre-flight smoke tests
uv run pytest tests/ --cov --cov-report=term-missing  # with coverage
```

## License

MIT
