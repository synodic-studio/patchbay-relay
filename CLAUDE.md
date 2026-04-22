# Stargate

Telegram bot bridge that routes messages to Claude Code sessions.

## Architecture

The bridge is modularized into a `stargate/` package with focused modules. `bridge.py` is the entrypoint that wires everything together and re-exports symbols for backward compatibility.

### Core modules

| File | Purpose |
|---|---|
| `bridge.py` | Entrypoint — Telegram handlers, command handlers, `run_claude()`, lifecycle (imports from `stargate/`) |
| `stargate/config.py` | All configuration: env vars, paths, constants, logging setup |
| `stargate/sessions.py` | Session persistence, sanitization, pending message management |
| `stargate/parser.py` | Claude CLI output parsing (JSON array, NDJSON, single-object) |
| `stargate/quota.py` | Quota/rate-limit detection and Forge handoff |
| `stargate/activity.py` | Structured JSON-lines activity logging |
| `stargate/projects.py` | Chat-to-project directory mapping |
| `auth.py` | Authentication state management (Apple Sign In + TOTP) |
| `auth_server.py` | FastAPI server for Sign in with Apple OIDC flow |
| `validate.py` | Pre-flight validation (syntax, imports, smoke tests for all modules) |

### Supporting files

| File | Purpose |
|---|---|
| `run.sh` | Entry point for bridge (uses `exec` to pass signals to Python) |
| `run_auth.sh` | Entry point for auth server + Cloudflare Tunnel (traps SIGTERM for clean shutdown) |

### Testing

329 tests across 16 test files. Run with `uv run pytest tests/`. Coverage: 83% overall, 100% on all `stargate/` modules.

When patching in tests, use the actual module path (e.g., `stargate.sessions.SESSION_DIR`, not `bridge.SESSION_DIR`) since functions in `stargate/` reference their own module's imports.

```bash
uv run pytest tests/ -q                    # quick run
uv run pytest tests/ --cov --cov-report=term-missing  # with coverage
uv run ruff check .                         # lint
uv run python validate.py                   # pre-flight smoke tests
```

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

## Telegram Commands

All commands are registered in `bridge.py` via `CommandHandler`. Auth-gated commands silently drop requests from unauthorized user IDs.

| Command | Auth required | Description |
|---|---|---|
| `/start` | No | Show the command list and your Telegram user ID. |
| `/auth` | No | Send an authentication link (Sign in with Apple). If already authenticated, shows session start time and expiry. |
| `/lock` | Yes | Lock your current session immediately. `/lock all` locks all active sessions across users. |
| `/clearnew` | No | Discard the current session ID for this topic and start a fresh one. Conversation history is lost. |
| `/setproject [path]` | No | Bind this topic to a project directory under `~/Developer`. Without an argument, shows an inline keyboard to pick from all subdirectories. Pass a path relative to `~/Developer` to set it directly. Session is reset on change. |
| `/project` | No | Show the project directory currently bound to this topic (and the agent name if one is configured). |
| `/kill` | Yes | Kill the active Claude subprocess for this topic. Session ID is preserved — the next message resumes in the same session. |
| `/restart` | Yes | Restart the bridge process (terminates all active Claude and remote-control processes, then exits non-zero so launchd respawns). Sends a ping to this topic after the new process starts. |
| `/remote_control` | Yes | Start `claude remote-control` in this topic's project directory, and report connection info. If one is already running, it is replaced. |
| `/remote_control stop` | Yes | Stop the running remote-control process. |
| `/ping` | No | Check liveness. Reports "pong" plus a list of any sessions currently running Claude, with elapsed time. |
| `/usage` | No | Show Claude Code quota via `ccusage` as two periods (active 5h block, current Mon→Mon week). Each period shows a token bar (used / cap) and a time bar (period elapsed). The 5h block cap comes from `ccusage --token-limit max`; the weekly cap is an estimate (env var `USAGE_WEEKLY_TOKEN_CAP`, default 3B, marked `(est)` in output) because Anthropic does not publish a weekly token cap for Max plans. |

### Session model

- Each forum topic (or DM chat) maps to a unique session key (`chat_id:thread_id`).
- Sessions carry a Claude `--resume` ID so consecutive messages share context.
- Sessions expire automatically after 3 days of inactivity.
- If Claude is already processing a message, new messages are queued and batched into a single follow-up invocation when the current one finishes.

### Photo / image handling

Sending a photo triggers `handle_photo`: the image is downloaded, and Claude is asked to read and describe it (or respond to the caption). The file is deleted after the response.

### Quota handoff

If Claude hits a quota/rate limit, the message is handed off to Forge (`~/Developer/Fanta/agents/dev/forge/queue/`) so it can be processed in the background and the response sent back to the same topic. Note: Forge is currently on ice (moved to drafts as of 2026-03-15) — the handoff code still writes the queue file but nothing processes it until Forge is reactivated.

## Python Environment

Managed with `uv`. Run `uv sync` to install dependencies. Scripts use `uv run` — no venv activation needed.

## Authentication

Auth is optional. Set `AUTH_REQUIRED=true` in `.env` to enforce it. Without it, all messages from `ALLOWED_USER_IDS` pass through.

### Data files

| File | Purpose |
|---|---|
| `auth/sessions.json` | Active sessions (Apple Sign In + TOTP) |
| `auth/totp_secrets.json` | TOTP secrets per Telegram user ID |
| `auth/auth_log.jsonl` | Append-only event log (auth, lock, expiry, etc.) |

### Session mechanics

- **Absolute expiry**: 30 days from `authenticated_at`
- **Inactivity timeout**: 7 days (env: `AUTH_INACTIVITY_TIMEOUT` in seconds)
- **IP pinning**: Apple Sign In sessions record the client IP at auth time. Any subsequent message from a different IP locks the session immediately.
- **Rate limiting**: 3 failed attempts within 5 minutes → 15-minute lockout (in-memory, resets on bridge restart)

### Apple Sign In flow

1. User sends `/auth` in Telegram.
2. `bridge.py` calls `auth.generate_auth_token()` — creates a 15-min one-time token, stores it in `auth/sessions.json` under `_pending_tokens`.
3. Bot replies with `https://auth.kj6.dev/login?token=<token>`.
4. User opens the link. `auth_server.py GET /login` verifies the token (without consuming it) and serves an HTML page with a "Sign in with Apple" button.
5. Clicking the button redirects to `https://appleid.apple.com/auth/authorize` with `response_mode=form_post` and `state=<token>`.
6. Apple authenticates the user and POSTs back to `https://auth.kj6.dev/callback` with `code`, `id_token`, and `state=<token>`.
7. `auth_server.py POST /callback`:
   - Consumes the auth token → maps back to the Telegram user ID.
   - Checks rate limit.
   - Verifies the Apple `id_token` JWT against Apple's public keys (`https://appleid.apple.com/auth/keys`, cached 1 hour).
   - Checks `apple_subject` (the `sub` claim) against the `APPLE_SUBJECT_ALLOWLIST` (loaded from Proton Pass / Keychain on startup).
   - Calls `auth.create_session()` → writes session to `auth/sessions.json` with `apple_subject`, `authenticated_at`, `last_seen`, `ip_address`.
8. User sees "Authenticated" page. Bridge auto-notifies the admin Telegram account of the new session.

Apple Developer setup required for the auth server: Services ID, Sign in with Apple enabled, registered redirect URL, and a `.p8` private key. See env vars at the top of `auth_server.py`.

### TOTP flow

TOTP is an alternative auth method (not a second factor). It creates a session independently without requiring Apple.

**Setup (run once per user):**

```bash
uv run setup_totp.py --user-id <telegram-user-id>
```

This generates a TOTP secret, prints a QR code for your authenticator app (Google Authenticator, Authy, 1Password, etc.), and writes the secret to:
- `auth/totp_secrets.json` — read by the bridge at verify time
- `~/.claude-bridge-totp` (mode 600) — backup copy

**Verification:**

`auth.authenticate_totp(telegram_user_id, code)` verifies the 6-digit code via pyotp (±1 window = ±30 seconds). On success it writes a session to `auth/sessions.json` with `auth_method: "totp"` (no IP pinning for TOTP sessions).

> **Note:** As of now, no `/totp <code>` bot command is wired in `bridge.py`. The `auth.authenticate_totp()` function is implemented in `auth.py` and the setup script is ready, but the Telegram command handler is not yet added.

### Admin commands

| Command | Effect |
|---|---|
| `/auth` | If unauthenticated: sends an Apple Sign In link. If already authenticated: shows session expiry. |
| `/lock` | Locks your own session immediately. |
| `/lock all` | Locks all active sessions. |
