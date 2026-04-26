# Apple Auth Implementation Reference

This document describes the Sign in with Apple (SIWA) and TOTP authentication system that was built for Stargate, how it worked, why it was separated from the bridge's message-handling path, and how to bring it back.

> **Status note.** The repo's git history was squashed to a single Initial commit when the project went public. The pre-squash state is preserved on the remote at the `v0-pre-public` tag. To browse the historical implementation: `git fetch origin v0-pre-public && git checkout v0-pre-public`. Any commit SHAs referenced below resolve only from that tag.

## Why It Was Separated

The auth layer was separated from `bridge.py` because:

- **Self-hosting friction.** Sign in with Apple requires an Apple Developer account ($99/year), a registered Services ID, a private key, domain verification, and a Cloudflare Tunnel (or equivalent reverse proxy). This makes self-hosting significantly harder.
- **Fragility.** The auth guards, rate limiting, IP pinning, and session management added failure modes to a system that already had enough. A bug in auth could lock the operator out of their own bridge.
- **Single-user context.** For a single-operator system, Telegram's own bot-token isolation (only people who know the bot token can find it) provides a reasonable baseline. The auth layer was defense-in-depth for a threat model that didn't justify the maintenance burden.

All auth module files (`auth.py`, `auth_server.py`, `setup_totp.py`, `run_auth.sh`, `run_apple_auth.sh`, `dev.kj6.auth-bridge.plist`, the `auth/` data dir, and the corresponding `tests/test_auth_*` files) were removed when the auth layer was retired. To revive auth, recover them from the `v0-pre-public` tag — this document describes the design they implemented.

## Architecture Overview

The auth system has three layers:

```
1. bridge.py          Auth guards: check session, gate commands, send auth links
2. auth_server.py     FastAPI OIDC server: serves login page, handles Apple callback
3. auth.py            State management: sessions, tokens, rate limiting, TOTP
```

Supporting infrastructure:

```
run_auth.sh                 Starts auth_server.py + Cloudflare Tunnel
run_apple_auth.sh           Starts auth_server.py only (tunnel managed separately)
dev.kj6.auth-bridge.plist   launchd service for persistent auth server
setup_totp.py               One-time TOTP secret generation with QR code
auth/                       Runtime state directory (gitignored)
  sessions.json             Active sessions (Apple + TOTP)
  totp_secrets.json          TOTP secrets per Telegram user ID
  auth_log.jsonl            Append-only audit log
  .sessions.lock            File lock for concurrent access
```

## How It Worked

### Sign in with Apple Flow

1. User sends `/auth` in Telegram.
2. `bridge.py` calls `auth.generate_auth_token(telegram_user_id)` -- creates a cryptographic one-time token (32 bytes, URL-safe base64), stores it in `auth/sessions.json` under `_pending_tokens` with a 15-minute TTL.
3. Bot replies with `https://auth.kj6.dev/login?token=<token>`.
4. User opens the link on any device. `auth_server.py GET /login` verifies the token exists and hasn't expired (without consuming it), then serves a minimal dark-themed HTML page with a "Sign in with Apple" button.
5. The button links to Apple's authorization endpoint:
   ```
   https://appleid.apple.com/auth/authorize
     ?client_id=<APPLE_SERVICE_ID>
     &redirect_uri=https://auth.kj6.dev/callback
     &response_type=code id_token
     &scope=name email
     &response_mode=form_post
     &state=<token>
   ```
6. Apple authenticates the user (Face ID, Touch ID, or password) and POSTs to `/callback` with `code`, `id_token`, and `state`.
7. `auth_server.py POST /callback`:
   - Consumes the auth token via `auth.consume_auth_token(state)` -- maps back to the Telegram user ID and deletes the token (one-time use).
   - Checks rate limit via `auth.is_rate_limited()`.
   - Verifies the Apple `id_token` JWT:
     - Fetches Apple's public keys from `https://appleid.apple.com/auth/keys` (cached 1 hour).
     - Matches the JWT's `kid` header to the correct public key.
     - Validates signature (RS256), audience (must match `APPLE_SERVICE_ID`), and issuer (must be `https://appleid.apple.com`).
   - Extracts the `sub` claim (Apple's stable user identifier). Rejects empty/missing `sub` with 403.
   - Checks the subject against `APPLE_SUBJECT_ALLOWLIST` (loaded from `pass show apple-subject-allowlist` at startup, falls back to env var).
   - Creates an authenticated session via `auth.create_session()`.
   - Fires notification callback to admin Telegram account.
8. User sees "Authenticated -- you can close this page and return to Telegram."

### Client Secret Generation

Apple doesn't use a traditional client secret. Instead, `auth_server.py` generates a JWT signed with the app's private key:

```python
payload = {
    "iss": APPLE_TEAM_ID,       # Your 10-char team ID
    "iat": now,
    "exp": now + 86400 * 180,   # 6 months max
    "aud": "https://appleid.apple.com",
    "sub": APPLE_SERVICE_ID,    # e.g. "dev.kj6.auth"
}
# Signed with ES256, kid=APPLE_KEY_ID
```

This JWT is used when exchanging the authorization code at Apple's token endpoint (though the current implementation uses the `id_token` from the form_post directly and doesn't call the token endpoint separately).

### TOTP Flow (Alternative Auth)

TOTP is an independent auth method, not a second factor. It creates sessions without requiring Apple.

**Setup (one-time):**
```bash
uv run setup_totp.py --user-id <telegram-user-id>
```
Generates a TOTP secret, displays a QR code for authenticator apps, and writes to `auth/totp_secrets.json` and `~/.claude-bridge-totp` (mode 600).

**Verification:**
`auth.authenticate_totp(telegram_user_id, code)` verifies the 6-digit code via pyotp with a valid_window of 1 (+-30 seconds). On success, creates a session with `auth_method: "totp"` (no IP pinning for TOTP sessions).

A late refinement added inline TOTP interception: when `AUTH_REQUIRED` was enabled and the user was unauthenticated, a plain 6-digit numeric message was treated as a TOTP code rather than triggering the auth-link flow.

### Session Model

Sessions are stored in `auth/sessions.json` with file locking (`fcntl.flock`) for concurrent access safety.

Each session contains:
```json
{
  "apple_subject": "000000.abc123...",
  "authenticated_at": 1741234567.0,
  "last_seen": 1741234567.0,
  "ip_address": "203.0.113.1",
  "locked": false
}
```

Session policies:
- **Absolute expiry:** 30 days from `authenticated_at`
- **Inactivity timeout:** 7 days since `last_seen` (configurable via `AUTH_INACTIVITY_TIMEOUT`)
- **IP pinning:** Apple Sign In sessions record the client IP. Any request from a different IP immediately locks the session and fires a notification.
- **Manual lock:** `/lock` locks your session, `/lock all` locks all sessions.

### Rate Limiting

In-memory (resets on bridge restart):
- 3 failed attempts within 5 minutes triggers a 15-minute lockout.
- Failed attempts: invalid Apple tokens, allowlist rejections, invalid TOTP codes.
- Cleared on successful authentication.

### User Allowlist

Two independent allowlists:
1. **`ALLOWED_USER_IDS`** (env var) -- Telegram user IDs permitted to interact with the bot at all. Checked before auth. Parsed at startup; exits on non-integer values.
2. **`APPLE_SUBJECT_ALLOWLIST`** -- Apple `sub` claim identifiers. Loaded from `pass show apple-subject-allowlist` at startup, falls back to env var. Controls which Apple IDs can create sessions.

### Notification Callback

`auth.py` supports an async notification callback (`set_notify_callback()`). The bridge registered `_auth_notify()` which sent Telegram messages to all admin user IDs on auth events:

| Event | Label |
|---|---|
| `authenticated` | NEW AUTH |
| `denied` | ACCESS DENIED |
| `ip_changed` | IP CHANGE |
| `locked` | SESSION LOCKED |
| `expired` | SESSION EXPIRED |
| `rate_limited` | (logged, notified) |
| `totp_failed` | (logged, notified) |

### Audit Log

All auth events are appended to `auth/auth_log.jsonl`:
```json
{"timestamp": 1741234567.0, "event": "authenticated", "telegram_user_id": 123456789, "details": "ip=203.0.113.1"}
```

## Which Files Handle What

| File | Responsibility |
|---|---|
| `auth.py` | All session state: create, validate, expire, lock, rate limit, TOTP verify, token generation/consumption, audit logging, notification dispatch |
| `auth_server.py` | FastAPI app with `/login` (serves HTML), `/callback` (Apple OIDC POST handler), `/health`. Generates Apple client secret JWTs. Verifies Apple id_tokens against Apple's public keys. Enforces subject allowlist. |
| `bridge.py` (auth code retired) | `_check_auth()` guard on message/photo handlers. `_send_auth_link()` token generation + link. `cmd_auth` / `cmd_lock` command handlers. `_auth_notify()` Telegram notification callback. `ALLOWED_USER_IDS` gate on all commands. |
| `stargate/config.py` (auth code retired) | `ALLOWED_USER_IDS` parsing, `AUTH_REQUIRED` flag, `AUTH_BASE_URL` constant. |
| `setup_totp.py` | CLI tool for TOTP secret generation with QR code display and verification. |
| `run_auth.sh` | Shell wrapper: starts auth_server.py and Cloudflare Tunnel, traps signals for clean shutdown. |
| `dev.kj6.auth-bridge.plist` | launchd plist for persistent auth server service. |

## Configuration

### Apple Developer Setup

1. Go to [developer.apple.com/account/resources/identifiers](https://developer.apple.com/account/resources/identifiers).
2. Create a **Services ID** (e.g. `dev.kj6.auth`).
3. Enable **Sign in with Apple** for the Services ID.
4. Register the domain (e.g. `auth.kj6.dev`) and the redirect URL (e.g. `https://auth.kj6.dev/callback`).
5. Create a **private key** for Sign in with Apple. Download the `.p8` file and note the Key ID.
6. Note your **Team ID** (visible on the Apple Developer membership page).

### Environment Variables

All auth-related env vars, as documented in `.env.example`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `AUTH_REQUIRED` | No | `false` | Set to `true` to enforce authentication |
| `AUTH_BASE_URL` | No | `https://auth.kj6.dev` | Public URL of the auth server |
| `AUTH_PORT` | No | `8443` | Port the auth server listens on |
| `AUTH_INACTIVITY_TIMEOUT` | No | `604800` (7 days) | Session inactivity timeout in seconds |
| `APPLE_SERVICE_ID` | Yes* | -- | Services ID from Apple Developer portal |
| `APPLE_TEAM_ID` | Yes* | -- | Apple Developer Team ID (10 chars) |
| `APPLE_KEY_ID` | Yes* | -- | Key ID for the `.p8` private key |
| `APPLE_PRIVATE_KEY_PATH` | Yes* | -- | Absolute path to the `.p8` private key file |
| `APPLE_SUBJECT_ALLOWLIST` | No | -- | Fallback: comma-separated Apple user IDs. Primary source: `pass show apple-subject-allowlist` |
| `ALLOWED_USER_IDS` | No | -- | Comma-separated Telegram user IDs permitted to use the bot |

*Required only when `AUTH_REQUIRED=true` and using Sign in with Apple.

### Cloudflare Tunnel

The auth server needs to be reachable from the internet so Apple can POST the callback. The original setup used a Cloudflare Tunnel:

```bash
cloudflared tunnel run auth-bridge
```

This routes `auth.kj6.dev` to `localhost:8443`. The tunnel is managed by launchd via the `dev.kj6.auth-bridge.plist` service, or can be started manually via `run_auth.sh`.

### Dependencies

Auth-specific Python packages (already in `pyproject.toml`):
- `PyJWT` -- JWT creation (client secret) and verification (Apple id_token)
- `httpx` -- async HTTP client for fetching Apple's public keys
- `fastapi` + `uvicorn` -- OIDC callback server
- `pyotp` -- TOTP generation and verification
- `qrcode` -- QR code display in `setup_totp.py` (optional, setup-time only)

## How to Bring It Back

### Step 1: Restore config values in `stargate/config.py`

Add back the removed constants (the exact diff is reachable from the `v0-pre-public` tag):

```python
# --- User allowlist ---
def _load_allowed_user_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    if not raw.strip():
        return set()
    parsed: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            parsed.add(int(token))
        except ValueError:
            print(f"ERROR: ALLOWED_USER_IDS contains non-integer value: {token!r}.", file=sys.stderr)
            sys.exit(1)
    return parsed

ALLOWED_USER_IDS = _load_allowed_user_ids()
AUTH_BASE_URL = os.environ.get("AUTH_BASE_URL", "https://auth.kj6.dev")
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "false").lower() == "true"
```

### Step 2: Restore auth guards in `bridge.py`

Add back (exact locations are reachable from the `v0-pre-public` tag):

1. `import auth` at the top.
2. Import `ALLOWED_USER_IDS`, `AUTH_BASE_URL`, `AUTH_REQUIRED` from `stargate.config`.
3. The `_check_auth()` and `_send_auth_link()` helper functions.
4. Auth checks at the top of `handle_message()` and `handle_photo()`:
   ```python
   if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
       return
   if not await _check_auth(update):
       await _send_auth_link(update)
       return
   ```
5. `ALLOWED_USER_IDS` guards on `cmd_kill`, `cmd_restart`, `cmd_remote_control`, `cmd_auth`, `cmd_lock`, `cmd_model`.
6. The `cmd_auth` and `cmd_lock` command handlers.
7. The `_auth_notify()` callback and `auth.set_notify_callback(_auth_notify)` in `post_init()`.
8. Register the command handlers: `CommandHandler("auth", cmd_auth)` and `CommandHandler("lock", cmd_lock)`.
9. Add `/auth` and `/lock` to the bot command list and `/start` help text.

### Step 3: Configure the environment

1. Set `AUTH_REQUIRED=true` in `.env`.
2. Set `ALLOWED_USER_IDS` to your Telegram user ID.
3. Set the `APPLE_*` variables (or skip Apple and use TOTP only).
4. Store your Apple subject allowlist in `pass`: `pass insert apple-subject-allowlist`.
5. Set up TOTP: `uv run setup_totp.py --user-id <your-telegram-user-id>`.

### Step 4: Start the auth server

```bash
# Option A: Run alongside bridge manually
./run_auth.sh

# Option B: Install as launchd service
# Edit dev.kj6.auth-bridge.plist (replace YOURUSER paths)
cp dev.kj6.auth-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/dev.kj6.auth-bridge.plist
```

### Step 5: Verify

1. Start the bridge with `AUTH_REQUIRED=true`.
2. Send any message in Telegram -- should get an auth link.
3. Click the link, sign in with Apple (or use `/totp <code>` if TOTP-only).
4. Confirm subsequent messages are processed normally.
5. Check `auth/auth_log.jsonl` for the `authenticated` event.

## Tests (also retired)

Three test files covered the auth system. They were removed alongside the auth code and are recoverable from the `v0-pre-public` tag:

- `tests/test_auth_comprehensive.py` — Session management, IP checking, rate limiting, auth tokens, TOTP.
- `tests/test_auth_server_allowlist.py` — Subject allowlist enforcement, empty-subject rejection, source failure logging.
- `tests/test_auth_server_endpoints.py` — FastAPI endpoint tests for `/login`, `/callback`, `/health`.

## Security Considerations

Notable hardening that was applied to the auth layer:

- **Empty `sub` claim rejection.** Tokens with missing or empty Apple subject get 403 + failed attempt, regardless of allowlist state.
- **Allowlist source failure logging.** If `pass` or keychain lookup fails, a WARNING is emitted so the operator knows the allowlist isn't loaded.
- **Fail-open warning.** If the allowlist is empty after all sources are tried, a startup WARNING makes the fail-open state explicit.
- **File permissions.** `auth/` directory is mode 700. `sessions.json` and `totp_secrets.json` are mode 600.
- **File locking.** `fcntl.flock` prevents concurrent writes to `sessions.json`.
- **Bot token wrapping.** The token is wrapped in `_SecretStr` so it never appears in tracebacks or repr output.
