"""Authentication state management for the Telegram bridge.

Manages Sign in with Apple sessions and TOTP: creation, validation, expiry,
IP change detection, and manual lock/unlock.
"""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("bridge.auth")

AUTH_DIR = Path(__file__).parent / "auth"
AUTH_DIR.mkdir(exist_ok=True)
os.chmod(AUTH_DIR, 0o700)
AUTH_STATE_FILE = AUTH_DIR / "sessions.json"
AUTH_LOG_FILE = AUTH_DIR / "auth_log.jsonl"

# 30-day session expiry (can tighten to 7 days if moving to VPS)
SESSION_EXPIRY_SECONDS = 30 * 24 * 60 * 60

# Inactivity timeout: auto-expire if no messages for this long (default 7 days)
INACTIVITY_TIMEOUT = int(
    os.environ.get("AUTH_INACTIVITY_TIMEOUT", str(7 * 24 * 60 * 60))
)

# Rate limiting: 3 failures in 5 min → locked out for 15 min
RATE_LIMIT_MAX_FAILURES = 3
RATE_LIMIT_WINDOW = 300  # 5 minutes
RATE_LIMIT_LOCKOUT = 900  # 15 minutes

# In-memory: telegram_user_id -> list of failure timestamps
_failed_attempts: dict[int, list[float]] = {}

# Optional async callback for auth event notifications (set by bridge.py)
_notify_callback = None


def set_notify_callback(callback) -> None:
    """Set an async callback for auth event notifications.

    callback(event_type: str, telegram_user_id: int, details: str) -> None
    """
    global _notify_callback
    _notify_callback = callback


def _notify(event_type: str, telegram_user_id: int, details: str = "") -> None:
    """Fire notification callback if set. Non-blocking."""
    if _notify_callback:
        try:
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(
                    _notify_callback(event_type, telegram_user_id, details)
                )
            else:
                loop.run_until_complete(
                    _notify_callback(event_type, telegram_user_id, details)
                )
        except Exception:
            logger.debug("Failed to send auth notification", exc_info=True)


def _load_state() -> dict:
    if AUTH_STATE_FILE.exists():
        try:
            return json.loads(AUTH_STATE_FILE.read_text())
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    AUTH_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    AUTH_STATE_FILE.chmod(0o600)


def _log_event(event_type: str, telegram_user_id: int, details: str = "") -> None:
    entry = {
        "timestamp": time.time(),
        "event": event_type,
        "telegram_user_id": telegram_user_id,
        "details": details,
    }
    with open(AUTH_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info("Auth event: %s for user %d %s", event_type, telegram_user_id, details)


def create_session(telegram_user_id: int, apple_subject: str, ip_address: str) -> None:
    """Create an authenticated session after successful Apple sign-in."""
    state = _load_state()
    state[str(telegram_user_id)] = {
        "apple_subject": apple_subject,
        "authenticated_at": time.time(),
        "last_seen": time.time(),
        "ip_address": ip_address,
        "locked": False,
    }
    _save_state(state)
    clear_rate_limit(telegram_user_id)
    _log_event("authenticated", telegram_user_id, f"ip={ip_address}")
    _notify("authenticated", telegram_user_id, f"IP: {ip_address}")


def is_authenticated(telegram_user_id: int) -> bool:
    """Check if a Telegram user has a valid, non-expired, non-locked session."""
    state = _load_state()
    session = state.get(str(telegram_user_id))
    if not session:
        return False
    if session.get("locked", False):
        return False
    now = time.time()
    if now - session["authenticated_at"] > SESSION_EXPIRY_SECONDS:
        _log_event("expired", telegram_user_id)
        return False
    last_seen = session.get("last_seen", session["authenticated_at"])
    if now - last_seen > INACTIVITY_TIMEOUT:
        _log_event(
            "inactive_expired",
            telegram_user_id,
            f"idle {(now - last_seen) / 3600:.1f}h",
        )
        _notify(
            "expired",
            telegram_user_id,
            f"Session expired due to inactivity ({(now - last_seen) / 3600:.1f}h)",
        )
        return False
    return True


def touch_session(telegram_user_id: int) -> None:
    """Update last_seen timestamp for a user's session."""
    state = _load_state()
    session = state.get(str(telegram_user_id))
    if session:
        session["last_seen"] = time.time()
        _save_state(state)


def check_ip(telegram_user_id: int, current_ip: str) -> bool:
    """Check if IP matches the authenticated session. Returns False if IP changed."""
    state = _load_state()
    session = state.get(str(telegram_user_id))
    if not session:
        return False
    stored_ip = session.get("ip_address")
    if stored_ip and stored_ip != current_ip:
        _log_event("ip_changed", telegram_user_id, f"from={stored_ip} to={current_ip}")
        # Lock the session on IP change
        session["locked"] = True
        session["lock_reason"] = f"IP changed: {stored_ip} -> {current_ip}"
        _save_state(state)
        _notify(
            "ip_changed",
            telegram_user_id,
            f"IP changed: {stored_ip} -> {current_ip} -- session locked",
        )
        return False
    # Update last_seen
    session["last_seen"] = time.time()
    _save_state(state)
    return True


def lock_all_sessions() -> int:
    """Lock all active sessions. Returns count of sessions locked."""
    state = _load_state()
    count = 0
    for uid, session in state.items():
        if not session.get("locked", False):
            session["locked"] = True
            session["lock_reason"] = "manual_lock"
            _log_event("locked", int(uid), "manual lock all")
            count += 1
    _save_state(state)
    return count


def lock_session(telegram_user_id: int) -> bool:
    """Lock a specific user's session. Returns True if session existed."""
    state = _load_state()
    session = state.get(str(telegram_user_id))
    if not session:
        return False
    session["locked"] = True
    session["lock_reason"] = "manual_lock"
    _save_state(state)
    _log_event("locked", telegram_user_id, "manual lock")
    return True


def get_session_info(telegram_user_id: int) -> dict | None:
    """Get session details for a user."""
    state = _load_state()
    return state.get(str(telegram_user_id))


def is_rate_limited(telegram_user_id: int) -> bool:
    """Check if a user is locked out due to too many failed auth attempts."""
    now = time.time()
    attempts = _failed_attempts.get(telegram_user_id, [])
    if not attempts:
        return False
    # Check if most recent lockout is still active
    if len(attempts) >= RATE_LIMIT_MAX_FAILURES:
        latest = attempts[-1]
        if now - latest < RATE_LIMIT_LOCKOUT:
            return True
    return False


def record_failed_attempt(telegram_user_id: int) -> bool:
    """Record a failed auth attempt. Returns True if user is now locked out."""
    now = time.time()
    attempts = _failed_attempts.setdefault(telegram_user_id, [])
    # Prune attempts outside the window
    cutoff = now - RATE_LIMIT_WINDOW
    _failed_attempts[telegram_user_id] = [t for t in attempts if t > cutoff]
    _failed_attempts[telegram_user_id].append(now)
    locked = len(_failed_attempts[telegram_user_id]) >= RATE_LIMIT_MAX_FAILURES
    if locked:
        _log_event(
            "rate_limited", telegram_user_id, f"locked out for {RATE_LIMIT_LOCKOUT}s"
        )
        _notify(
            "rate_limited",
            telegram_user_id,
            f"Locked out after {RATE_LIMIT_MAX_FAILURES} failed attempts",
        )
    return locked


def clear_rate_limit(telegram_user_id: int) -> None:
    """Clear rate limit state for a user (e.g. after successful auth)."""
    _failed_attempts.pop(telegram_user_id, None)


def generate_auth_token(telegram_user_id: int) -> str:
    """Generate a one-time token linking Telegram user to auth flow.

    The token is stored and verified when Apple callback completes,
    ensuring the Apple sign-in maps to the correct Telegram user.
    """
    import secrets

    token = secrets.token_urlsafe(32)
    state = _load_state()
    # Store pending auth tokens separately
    pending = state.get("_pending_tokens", {})
    pending[token] = {
        "telegram_user_id": telegram_user_id,
        "created_at": time.time(),
    }
    # Clean expired pending tokens (15 min lifetime)
    pending = {k: v for k, v in pending.items() if time.time() - v["created_at"] < 900}
    state["_pending_tokens"] = pending
    _save_state(state)
    return token


def check_auth_token(token: str) -> int | None:
    """Check if a pending auth token is valid without consuming it."""
    state = _load_state()
    pending = state.get("_pending_tokens", {})
    entry = pending.get(token)
    if not entry:
        return None
    if time.time() - entry["created_at"] > 900:
        return None
    return entry["telegram_user_id"]


def consume_auth_token(token: str) -> int | None:
    """Verify and consume a pending auth token. Returns Telegram user ID or None."""
    state = _load_state()
    pending = state.get("_pending_tokens", {})
    entry = pending.get(token)
    if not entry:
        return None
    if time.time() - entry["created_at"] > 900:  # 15 min expiry
        del pending[token]
        _save_state(state)
        return None
    telegram_user_id = entry["telegram_user_id"]
    del pending[token]
    state["_pending_tokens"] = pending
    _save_state(state)
    return telegram_user_id


# --- TOTP Authentication ---

TOTP_SECRETS_FILE = AUTH_DIR / "totp_secrets.json"


def _load_totp_secrets() -> dict:
    if TOTP_SECRETS_FILE.exists():
        try:
            return json.loads(TOTP_SECRETS_FILE.read_text())
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _save_totp_secrets(secrets: dict) -> None:
    TOTP_SECRETS_FILE.write_text(json.dumps(secrets, indent=2) + "\n")
    TOTP_SECRETS_FILE.chmod(0o600)


def setup_totp(telegram_user_id: int) -> tuple[str, str]:
    """Generate a TOTP secret for a user. Returns (secret, provisioning_uri)."""
    import pyotp

    secret = pyotp.random_base32()
    secrets = _load_totp_secrets()
    secrets[str(telegram_user_id)] = {"secret": secret, "created_at": time.time()}
    _save_totp_secrets(secrets)
    _log_event("totp_setup", telegram_user_id)
    _notify("totp_setup", telegram_user_id, "TOTP secret configured")
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=str(telegram_user_id), issuer_name="ClaudeBridge"
    )
    return secret, uri


def has_totp(telegram_user_id: int) -> bool:
    """Check if a user has TOTP configured."""
    secrets = _load_totp_secrets()
    return str(telegram_user_id) in secrets


def verify_totp(telegram_user_id: int, code: str) -> bool:
    """Verify a TOTP code. Returns True if valid."""
    import pyotp

    secrets = _load_totp_secrets()
    entry = secrets.get(str(telegram_user_id))
    if not entry:
        return False
    totp = pyotp.TOTP(entry["secret"])
    return totp.verify(code, valid_window=1)


def authenticate_totp(telegram_user_id: int, code: str) -> bool:
    """Verify TOTP code and create a session if valid. Returns True on success."""
    if is_rate_limited(telegram_user_id):
        return False
    if not verify_totp(telegram_user_id, code):
        record_failed_attempt(telegram_user_id)
        _log_event("totp_failed", telegram_user_id)
        _notify("totp_failed", telegram_user_id, "Invalid TOTP code")
        return False
    # Create session (no Apple subject or IP for TOTP-based auth)
    state = _load_state()
    state[str(telegram_user_id)] = {
        "auth_method": "totp",
        "authenticated_at": time.time(),
        "last_seen": time.time(),
        "locked": False,
    }
    _save_state(state)
    clear_rate_limit(telegram_user_id)
    _log_event("totp_authenticated", telegram_user_id)
    _notify("authenticated", telegram_user_id, "via TOTP")
    return True


def remove_totp(telegram_user_id: int) -> bool:
    """Remove TOTP secret for a user. Returns True if existed."""
    secrets = _load_totp_secrets()
    if str(telegram_user_id) not in secrets:
        return False
    del secrets[str(telegram_user_id)]
    _save_totp_secrets(secrets)
    _log_event("totp_removed", telegram_user_id)
    _notify("totp_removed", telegram_user_id, "TOTP secret removed")
    return True
