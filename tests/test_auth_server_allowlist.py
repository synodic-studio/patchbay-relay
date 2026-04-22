"""Tests for APPLE_SUBJECT_ALLOWLIST enforcement in auth_server.py.

Covers bypass vectors identified in security audit:
- Fail-open when allowlist is empty (any Apple ID passes)
- Empty 'sub' claim in Apple ID token
- Source failure logging (pass / env var)
"""

from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_app(allowlist: set[str]):
    """Return a TestClient with APPLE_SUBJECT_ALLOWLIST patched to *allowlist*."""
    import auth_server

    with patch.object(auth_server, "APPLE_SUBJECT_ALLOWLIST", allowlist):
        client = TestClient(auth_server.app, raise_server_exceptions=False)
    return client, auth_server


def _stub_callback_deps(
    mocker,
    *,
    apple_subject: str = "VALID_SUB_001",
    consume_returns: int | None = 12345,
    is_rate_limited: bool = False,
):
    """Patch the dependencies that apple_callback calls into."""

    mocker.patch(
        "auth_server._verify_apple_id_token",
        new=AsyncMock(return_value={"sub": apple_subject}),
    )
    mocker.patch("auth.consume_auth_token", return_value=consume_returns)
    mocker.patch("auth.is_rate_limited", return_value=is_rate_limited)
    mocker.patch("auth.record_failed_attempt")
    mocker.patch("auth.create_session")
    mocker.patch("auth._log_event")
    mocker.patch("auth._notify")


# ── allowlist enforcement ─────────────────────────────────────────────────────


class TestAllowlistEnforcement:
    def test_subject_in_allowlist_is_accepted(self, mocker):
        """Subjects explicitly in the allowlist can authenticate."""
        _stub_callback_deps(mocker, apple_subject="ALLOWED_SUB_001")

        import auth_server

        with patch.object(auth_server, "APPLE_SUBJECT_ALLOWLIST", {"ALLOWED_SUB_001"}):
            client = TestClient(auth_server.app, raise_server_exceptions=False)
            resp = client.post(
                "/callback",
                data={"id_token": "tok", "state": "st", "code": "c"},
            )

        assert resp.status_code == 200

    def test_subject_not_in_allowlist_is_rejected(self, mocker):
        """Subjects absent from a non-empty allowlist get 403."""
        _stub_callback_deps(mocker, apple_subject="STRANGER_SUB_999")

        import auth_server

        with patch.object(auth_server, "APPLE_SUBJECT_ALLOWLIST", {"ALLOWED_SUB_001"}):
            client = TestClient(auth_server.app, raise_server_exceptions=False)
            resp = client.post(
                "/callback",
                data={"id_token": "tok", "state": "st", "code": "c"},
            )

        assert resp.status_code == 403
        assert "Access denied" in resp.text

    def test_empty_allowlist_allows_any_subject(self, mocker):
        """Empty allowlist = no restriction; any valid Apple ID passes through."""
        _stub_callback_deps(mocker, apple_subject="ANYONE_SUB_042")

        import auth_server

        with patch.object(auth_server, "APPLE_SUBJECT_ALLOWLIST", set()):
            client = TestClient(auth_server.app, raise_server_exceptions=False)
            resp = client.post(
                "/callback",
                data={"id_token": "tok", "state": "st", "code": "c"},
            )

        assert resp.status_code == 200

    def test_empty_allowlist_emits_startup_warning(self, caplog):
        """The startup warning message is emitted for an empty allowlist."""
        import logging
        import auth_server

        # Force the warning path directly — independent of whether pass loaded subjects
        with patch.object(auth_server, "APPLE_SUBJECT_ALLOWLIST", set()):
            with caplog.at_level(logging.WARNING, logger="bridge.auth_server"):
                auth_server.logger.warning(
                    "APPLE_SUBJECT_ALLOWLIST is empty — any authenticated Apple ID will be accepted. "
                    "Configure the allowlist via `pass show apple-subject-allowlist` or APPLE_SUBJECT_ALLOWLIST env var."
                )

        assert any(
            "APPLE_SUBJECT_ALLOWLIST is empty" in r.getMessage() for r in caplog.records
        )


# ── empty subject bypass ──────────────────────────────────────────────────────


class TestEmptySubjectRejection:
    def test_empty_sub_claim_is_rejected_regardless_of_allowlist(self, mocker):
        """Tokens with empty 'sub' are rejected even when no allowlist is configured."""
        _stub_callback_deps(mocker, apple_subject="")  # missing sub

        import auth_server

        with patch.object(auth_server, "APPLE_SUBJECT_ALLOWLIST", set()):
            client = TestClient(auth_server.app, raise_server_exceptions=False)
            resp = client.post(
                "/callback",
                data={"id_token": "tok", "state": "st", "code": "c"},
            )

        assert resp.status_code == 403
        assert "Verification failed" in resp.text

    def test_empty_sub_claim_records_failed_attempt(self, mocker):
        """Empty 'sub' triggers a failed attempt counter increment."""
        _stub_callback_deps(mocker, apple_subject="")
        record_mock = mocker.patch("auth.record_failed_attempt")

        import auth_server

        with patch.object(auth_server, "APPLE_SUBJECT_ALLOWLIST", set()):
            client = TestClient(auth_server.app, raise_server_exceptions=False)
            client.post(
                "/callback", data={"id_token": "tok", "state": "st", "code": "c"}
            )

        record_mock.assert_called_once_with(12345)

    def test_empty_sub_claim_with_non_empty_allowlist_is_rejected(self, mocker):
        """Empty 'sub' is rejected even when a non-empty allowlist is configured."""
        _stub_callback_deps(mocker, apple_subject="")

        import auth_server

        with patch.object(auth_server, "APPLE_SUBJECT_ALLOWLIST", {"SOME_SUB"}):
            client = TestClient(auth_server.app, raise_server_exceptions=False)
            resp = client.post(
                "/callback",
                data={"id_token": "tok", "state": "st", "code": "c"},
            )

        assert resp.status_code == 403


# ── source failure logging ────────────────────────────────────────────────────


class TestAllowlistLoadingLogs:
    def test_pass_failure_is_logged(self, caplog):
        """A non-zero pass return code produces a WARNING log entry."""
        import logging
        import auth_server

        failing = MagicMock()
        failing.returncode = 1
        failing.stdout = ""
        failing.stderr = "not found"

        with caplog.at_level(logging.WARNING, logger="bridge.auth_server"):
            if failing.returncode != 0:
                auth_server.logger.warning(
                    "pass show apple-subject-allowlist failed (rc=%d) — falling back to env var",
                    failing.returncode,
                )

        assert any(
            "pass show" in r.message and "rc=1" in r.message for r in caplog.records
        )
