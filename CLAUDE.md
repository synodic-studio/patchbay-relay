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
