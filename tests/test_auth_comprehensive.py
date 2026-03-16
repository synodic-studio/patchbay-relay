"""Comprehensive tests for the auth module.

Covers session management, IP checking, rate limiting, auth tokens, and TOTP.
"""

import json
import time
from unittest.mock import patch

import pyotp
import pytest

import auth


@pytest.fixture
def auth_state(tmp_auth_state):
    """Extend tmp_auth_state to also patch TOTP_SECRETS_FILE.

    The base fixture patches AUTH_DIR, AUTH_STATE_FILE, AUTH_LOG_FILE, and
    _AUTH_LOCK_FILE.  TOTP_SECRETS_FILE is computed at import time from the
    original AUTH_DIR, so it needs its own patch.
    """
    tmp_totp_file = tmp_auth_state["dir"] / "totp_secrets.json"
    with patch.object(auth, "TOTP_SECRETS_FILE", tmp_totp_file):
        tmp_auth_state["totp_file"] = tmp_totp_file
        yield tmp_auth_state


# ---------------------------------------------------------------------------
# 1. create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_creates_session_with_correct_fields(self, auth_state):
        auth.create_session(111, "apple.sub.001", "1.2.3.4")

        state = json.loads(auth_state["state_file"].read_text())
        session = state["111"]
        assert session["apple_subject"] == "apple.sub.001"
        assert session["ip_address"] == "1.2.3.4"
        assert session["locked"] is False
        assert "authenticated_at" in session
        assert "last_seen" in session

    def test_clears_rate_limit_after_creation(self, auth_state):
        # Accumulate some failed attempts first
        auth.record_failed_attempt(222)
        auth.record_failed_attempt(222)
        assert auth._failed_attempts.get(222)

        auth.create_session(222, "apple.sub.002", "5.6.7.8")
        assert 222 not in auth._failed_attempts

    def test_logs_event(self, auth_state):
        auth.create_session(333, "apple.sub.003", "10.0.0.1")

        log_text = auth_state["log_file"].read_text()
        entries = [json.loads(line) for line in log_text.strip().splitlines()]
        auth_events = [e for e in entries if e["event"] == "authenticated"]
        assert len(auth_events) == 1
        assert auth_events[0]["telegram_user_id"] == 333
        assert "10.0.0.1" in auth_events[0]["details"]


# ---------------------------------------------------------------------------
# 2. is_authenticated
# ---------------------------------------------------------------------------


class TestIsAuthenticated:
    def test_valid_session_returns_true(self, auth_state):
        auth.create_session(100, "sub", "1.1.1.1")
        assert auth.is_authenticated(100) is True

    def test_locked_session_returns_false(self, auth_state):
        auth.create_session(101, "sub", "1.1.1.1")
        auth.lock_session(101)
        assert auth.is_authenticated(101) is False

    def test_expired_session_returns_false(self, auth_state):
        auth.create_session(102, "sub", "1.1.1.1")
        expired_time = time.time() - auth.SESSION_EXPIRY_SECONDS - 1
        state = json.loads(auth_state["state_file"].read_text())
        state["102"]["authenticated_at"] = expired_time
        state["102"]["last_seen"] = time.time()
        auth_state["state_file"].write_text(json.dumps(state))

        assert auth.is_authenticated(102) is False

    def test_inactive_session_returns_false(self, auth_state):
        auth.create_session(103, "sub", "1.1.1.1")
        old_last_seen = time.time() - auth.INACTIVITY_TIMEOUT - 1
        state = json.loads(auth_state["state_file"].read_text())
        state["103"]["last_seen"] = old_last_seen
        auth_state["state_file"].write_text(json.dumps(state))

        assert auth.is_authenticated(103) is False

    def test_nonexistent_user_returns_false(self, auth_state):
        assert auth.is_authenticated(999) is False

    def test_boundary_at_exactly_expiry_time(self, auth_state):
        """Session at exactly SESSION_EXPIRY_SECONDS should still be valid.

        The check is `>` (strictly greater), not `>=`.
        """
        frozen_now = 1_700_000_000.0
        auth.create_session(104, "sub", "1.1.1.1")

        # Set authenticated_at so that now - authenticated_at == exactly SESSION_EXPIRY_SECONDS
        state = json.loads(auth_state["state_file"].read_text())
        state["104"]["authenticated_at"] = frozen_now - auth.SESSION_EXPIRY_SECONDS
        state["104"]["last_seen"] = frozen_now  # fresh, no inactivity
        auth_state["state_file"].write_text(json.dumps(state))

        # Freeze time so the `>` check sees exactly the boundary
        with patch("auth.time") as mock_time:
            mock_time.time.return_value = frozen_now
            # now - authenticated_at == SESSION_EXPIRY_SECONDS (not >), so valid
            assert auth.is_authenticated(104) is True


# ---------------------------------------------------------------------------
# 3. touch_session
# ---------------------------------------------------------------------------


class TestTouchSession:
    def test_updates_last_seen(self, auth_state):
        auth.create_session(200, "sub", "1.1.1.1")
        state = json.loads(auth_state["state_file"].read_text())
        original_last_seen = state["200"]["last_seen"]

        # Nudge the clock forward
        with patch("auth.time") as mock_time:
            mock_time.time.return_value = original_last_seen + 60
            auth.touch_session(200)

        state = json.loads(auth_state["state_file"].read_text())
        assert state["200"]["last_seen"] == original_last_seen + 60

    def test_noop_for_nonexistent_user(self, auth_state):
        # Should not raise or create a session
        auth.touch_session(999)
        state_file = auth_state["state_file"]
        if state_file.exists():
            state = json.loads(state_file.read_text())
        else:
            state = {}
        assert "999" not in state


# ---------------------------------------------------------------------------
# 4. check_ip
# ---------------------------------------------------------------------------


class TestCheckIp:
    def test_same_ip_returns_true_and_updates_last_seen(self, auth_state):
        auth.create_session(300, "sub", "10.0.0.1")
        state = json.loads(auth_state["state_file"].read_text())
        original_last_seen = state["300"]["last_seen"]

        with patch("auth.time") as mock_time:
            mock_time.time.return_value = original_last_seen + 120
            result = auth.check_ip(300, "10.0.0.1")

        assert result is True
        state = json.loads(auth_state["state_file"].read_text())
        assert state["300"]["last_seen"] == original_last_seen + 120

    def test_different_ip_returns_false_and_locks_session(self, auth_state):
        auth.create_session(301, "sub", "10.0.0.1")
        result = auth.check_ip(301, "99.99.99.99")

        assert result is False
        state = json.loads(auth_state["state_file"].read_text())
        assert state["301"]["locked"] is True
        assert "IP changed" in state["301"]["lock_reason"]

    def test_nonexistent_user_returns_false(self, auth_state):
        assert auth.check_ip(999, "1.2.3.4") is False

    def test_totp_session_no_ip_field(self, auth_state):
        """TOTP sessions have no ip_address field.

        When stored_ip is None (no ip_address key), the check
        `stored_ip and stored_ip != current_ip` short-circuits to False,
        so check_ip returns True and updates last_seen.
        """
        auth.authenticate_totp.__wrapped__ if hasattr(auth.authenticate_totp, "__wrapped__") else None
        # Manually create a TOTP-style session (no ip_address key)
        state = {
            "302": {
                "auth_method": "totp",
                "authenticated_at": time.time(),
                "last_seen": time.time(),
                "locked": False,
            }
        }
        auth_state["state_file"].write_text(json.dumps(state))

        result = auth.check_ip(302, "anything.here")
        assert result is True
        # last_seen should be updated
        state = json.loads(auth_state["state_file"].read_text())
        assert "last_seen" in state["302"]


# ---------------------------------------------------------------------------
# 5. lock_session / lock_all_sessions
# ---------------------------------------------------------------------------


class TestLockSession:
    def test_lock_specific_user(self, auth_state):
        auth.create_session(400, "sub", "1.1.1.1")
        result = auth.lock_session(400)
        assert result is True
        assert auth.is_authenticated(400) is False

    def test_lock_nonexistent_user_returns_false(self, auth_state):
        assert auth.lock_session(999) is False


class TestLockAllSessions:
    def test_lock_multiple_sessions(self, auth_state):
        auth.create_session(500, "sub1", "1.1.1.1")
        auth.create_session(501, "sub2", "2.2.2.2")
        auth.create_session(502, "sub3", "3.3.3.3")

        count = auth.lock_all_sessions()
        assert count == 3
        assert auth.is_authenticated(500) is False
        assert auth.is_authenticated(501) is False
        assert auth.is_authenticated(502) is False

    def test_lock_all_with_no_sessions_returns_zero(self, auth_state):
        count = auth.lock_all_sessions()
        assert count == 0

    def test_lock_all_skips_already_locked(self, auth_state):
        auth.create_session(510, "sub1", "1.1.1.1")
        auth.create_session(511, "sub2", "2.2.2.2")
        auth.lock_session(510)

        count = auth.lock_all_sessions()
        # Only 511 should be newly locked
        assert count == 1


# ---------------------------------------------------------------------------
# 6. Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_no_attempts_not_limited(self, auth_state):
        assert auth.is_rate_limited(600) is False

    def test_three_failures_within_window_locks(self, auth_state):
        auth.record_failed_attempt(601)
        auth.record_failed_attempt(601)
        locked = auth.record_failed_attempt(601)
        assert locked is True
        assert auth.is_rate_limited(601) is True

    def test_failures_outside_window_are_pruned(self, auth_state):
        uid = 602
        old_time = time.time() - auth.RATE_LIMIT_WINDOW - 10
        auth._failed_attempts[uid] = [old_time, old_time + 1]

        # This call should prune the old ones and add one fresh
        locked = auth.record_failed_attempt(uid)
        assert locked is False
        # Only 1 recent attempt should remain
        assert len(auth._failed_attempts[uid]) == 1

    def test_clear_rate_limit_resets_state(self, auth_state):
        auth.record_failed_attempt(603)
        auth.record_failed_attempt(603)
        auth.record_failed_attempt(603)
        assert auth.is_rate_limited(603) is True

        auth.clear_rate_limit(603)
        assert auth.is_rate_limited(603) is False
        assert 603 not in auth._failed_attempts


# ---------------------------------------------------------------------------
# 7. Auth tokens
# ---------------------------------------------------------------------------


class TestAuthTokens:
    def test_generate_returns_string(self, auth_state):
        token = auth.generate_auth_token(700)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_check_validates_unexpired_token(self, auth_state):
        token = auth.generate_auth_token(701)
        uid = auth.check_auth_token(token)
        assert uid == 701

    def test_check_returns_none_for_expired(self, auth_state):
        token = auth.generate_auth_token(702)

        # Make the token expired by altering state
        state = json.loads(auth_state["state_file"].read_text())
        state["_pending_tokens"][token]["created_at"] = time.time() - 901
        auth_state["state_file"].write_text(json.dumps(state))

        assert auth.check_auth_token(token) is None

    def test_consume_returns_uid_and_removes_token(self, auth_state):
        token = auth.generate_auth_token(703)
        uid = auth.consume_auth_token(token)
        assert uid == 703

        # Token should be gone now
        state = json.loads(auth_state["state_file"].read_text())
        assert token not in state.get("_pending_tokens", {})

    def test_consume_already_consumed_returns_none(self, auth_state):
        token = auth.generate_auth_token(704)
        auth.consume_auth_token(token)
        assert auth.consume_auth_token(token) is None

    def test_expired_tokens_cleaned_on_generate(self, auth_state):
        # Create a token, then backdate it to make it expired
        old_token = auth.generate_auth_token(705)
        state = json.loads(auth_state["state_file"].read_text())
        state["_pending_tokens"][old_token]["created_at"] = time.time() - 1000
        auth_state["state_file"].write_text(json.dumps(state))

        # Generating a new token should clean the expired one
        auth.generate_auth_token(706)
        state = json.loads(auth_state["state_file"].read_text())
        assert old_token not in state["_pending_tokens"]


# ---------------------------------------------------------------------------
# 8. TOTP
# ---------------------------------------------------------------------------


class TestTotp:
    def test_setup_creates_secret(self, auth_state):
        secret, uri = auth.setup_totp(800)
        assert isinstance(secret, str)
        assert len(secret) > 0
        assert "ClaudeBridge" in uri
        assert "800" in uri

    def test_has_totp_true_after_setup(self, auth_state):
        assert auth.has_totp(800) is False
        auth.setup_totp(800)
        assert auth.has_totp(800) is True

    def test_verify_with_valid_code(self, auth_state):
        secret, _ = auth.setup_totp(801)
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert auth.verify_totp(801, code) is True

    def test_verify_with_invalid_code(self, auth_state):
        auth.setup_totp(802)
        assert auth.verify_totp(802, "000000") is False

    def test_authenticate_creates_session_on_success(self, auth_state):
        secret, _ = auth.setup_totp(803)
        totp = pyotp.TOTP(secret)
        code = totp.now()

        result = auth.authenticate_totp(803, code)
        assert result is True
        assert auth.is_authenticated(803) is True

        # Session should have auth_method = "totp" and no ip_address
        state = json.loads(auth_state["state_file"].read_text())
        session = state["803"]
        assert session["auth_method"] == "totp"
        assert "ip_address" not in session

    def test_authenticate_records_failure_on_bad_code(self, auth_state):
        auth.setup_totp(804)
        result = auth.authenticate_totp(804, "000000")
        assert result is False
        assert auth.is_authenticated(804) is False

        # Should have recorded a failed attempt
        assert len(auth._failed_attempts.get(804, [])) == 1

    def test_authenticate_respects_rate_limit(self, auth_state):
        secret, _ = auth.setup_totp(805)
        totp = pyotp.TOTP(secret)

        # Exhaust rate limit with bad codes
        auth.record_failed_attempt(805)
        auth.record_failed_attempt(805)
        auth.record_failed_attempt(805)
        assert auth.is_rate_limited(805) is True

        # Even a valid code should be rejected
        code = totp.now()
        result = auth.authenticate_totp(805, code)
        assert result is False

    def test_remove_totp_removes_secret(self, auth_state):
        auth.setup_totp(806)
        assert auth.has_totp(806) is True

        result = auth.remove_totp(806)
        assert result is True
        assert auth.has_totp(806) is False

    def test_remove_totp_nonexistent_returns_false(self, auth_state):
        assert auth.remove_totp(999) is False
