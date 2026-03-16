"""Tests for auth_server.py FastAPI endpoints: /login, /callback, /health,
and the internal helpers _get_apple_client_secret, _fetch_apple_keys,
_verify_apple_id_token."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_auth(tmp_auth_state):
    """Every test gets isolated auth state."""


@pytest.fixture
def client():
    """TestClient wrapping the FastAPI app from auth_server."""
    import auth_server

    return TestClient(auth_server.app)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------


class TestLoginEndpoint:
    def test_missing_token_returns_400(self, client):
        resp = client.get("/login")
        assert resp.status_code == 400
        assert "Missing auth token" in resp.text

    def test_missing_token_explicit_empty(self, client):
        resp = client.get("/login?token=")
        assert resp.status_code == 400
        assert "Missing auth token" in resp.text

    def test_invalid_token_returns_400(self, client):
        resp = client.get("/login?token=bogus-token-not-in-state")
        assert resp.status_code == 400
        assert "expired" in resp.text.lower() or "invalid" in resp.text.lower()

    def test_valid_token_renders_login_page(self, client):
        import auth

        token = auth.generate_auth_token(42)
        resp = client.get(f"/login?token={token}")
        assert resp.status_code == 200
        assert "Sign in with Apple" in resp.text
        assert token in resp.text  # state should be in the auth URL


# ---------------------------------------------------------------------------
# /callback
# ---------------------------------------------------------------------------


class TestCallbackEndpoint:
    def test_error_param_returns_400(self, client):
        resp = client.post("/callback", data={"error": "user_cancelled"})
        assert resp.status_code == 400
        assert "user_cancelled" in resp.text

    def test_missing_id_token_returns_400(self, client):
        resp = client.post("/callback", data={"state": "some-state"})
        assert resp.status_code == 400
        assert "Missing required" in resp.text

    def test_missing_state_returns_400(self, client):
        resp = client.post("/callback", data={"id_token": "some-jwt"})
        assert resp.status_code == 400
        assert "Missing required" in resp.text

    def test_expired_state_token_returns_400(self, client):
        resp = client.post(
            "/callback",
            data={"id_token": "some-jwt", "state": "expired-token-123"},
        )
        assert resp.status_code == 400
        assert "expired" in resp.text.lower() or "already used" in resp.text.lower()

    def test_rate_limited_user_returns_429(self, client):
        import auth

        token = auth.generate_auth_token(42)
        # Force rate limit
        for _ in range(5):
            auth.record_failed_attempt(42)

        with patch("auth_server._verify_apple_id_token", new_callable=AsyncMock):
            resp = client.post(
                "/callback",
                data={"id_token": "jwt", "state": token},
            )
        assert resp.status_code == 429
        assert "Too many" in resp.text

    def test_apple_token_verification_failure_returns_400(self, client):
        import auth

        token = auth.generate_auth_token(42)
        with patch(
            "auth_server._verify_apple_id_token",
            new_callable=AsyncMock,
            side_effect=ValueError("bad sig"),
        ):
            resp = client.post(
                "/callback",
                data={"id_token": "bad-jwt", "state": token},
            )
        assert resp.status_code == 400
        assert "Verification failed" in resp.text

    def test_missing_sub_claim_returns_403(self, client):
        import auth

        token = auth.generate_auth_token(42)
        with patch(
            "auth_server._verify_apple_id_token",
            new_callable=AsyncMock,
            return_value={"sub": ""},
        ):
            resp = client.post(
                "/callback",
                data={"id_token": "jwt", "state": token},
            )
        assert resp.status_code == 403
        assert "Verification failed" in resp.text

    def test_subject_not_in_allowlist_returns_403(self, client, monkeypatch):
        import auth
        import auth_server

        monkeypatch.setattr(auth_server, "APPLE_SUBJECT_ALLOWLIST", {"allowed-sub"})
        token = auth.generate_auth_token(42)
        with patch(
            "auth_server._verify_apple_id_token",
            new_callable=AsyncMock,
            return_value={"sub": "not-allowed-sub"},
        ):
            resp = client.post(
                "/callback",
                data={"id_token": "jwt", "state": token},
            )
        assert resp.status_code == 403
        assert "not authorized" in resp.text

    def test_successful_authentication(self, client, monkeypatch):
        import auth
        import auth_server

        monkeypatch.setattr(auth_server, "APPLE_SUBJECT_ALLOWLIST", set())
        token = auth.generate_auth_token(42)
        with patch(
            "auth_server._verify_apple_id_token",
            new_callable=AsyncMock,
            return_value={"sub": "apple-subject-xyz"},
        ):
            resp = client.post(
                "/callback",
                data={"id_token": "jwt", "state": token},
            )
        assert resp.status_code == 200
        assert "Authenticated" in resp.text
        assert "close this page" in resp.text
        # Verify session was created
        assert auth.is_authenticated(42)

    def test_successful_auth_with_allowlist(self, client, monkeypatch):
        import auth
        import auth_server

        monkeypatch.setattr(
            auth_server, "APPLE_SUBJECT_ALLOWLIST", {"apple-sub-allowed"}
        )
        token = auth.generate_auth_token(42)
        with patch(
            "auth_server._verify_apple_id_token",
            new_callable=AsyncMock,
            return_value={"sub": "apple-sub-allowed"},
        ):
            resp = client.post(
                "/callback",
                data={"id_token": "jwt", "state": token},
            )
        assert resp.status_code == 200
        assert auth.is_authenticated(42)

    def test_x_forwarded_for_is_used(self, client, monkeypatch):
        import auth
        import auth_server

        monkeypatch.setattr(auth_server, "APPLE_SUBJECT_ALLOWLIST", set())
        token = auth.generate_auth_token(42)
        with patch(
            "auth_server._verify_apple_id_token",
            new_callable=AsyncMock,
            return_value={"sub": "apple-sub-xyz"},
        ):
            resp = client.post(
                "/callback",
                data={"id_token": "jwt", "state": token},
                headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"},
            )
        assert resp.status_code == 200
        session = auth.get_session_info(42)
        assert session["ip_address"] == "1.2.3.4"


# ---------------------------------------------------------------------------
# _get_apple_client_secret
# ---------------------------------------------------------------------------


class TestGetAppleClientSecret:
    def test_raises_file_not_found(self, monkeypatch):
        import auth_server

        monkeypatch.setattr(auth_server, "APPLE_PRIVATE_KEY_PATH", "/tmp/nonexistent.p8")
        with pytest.raises(FileNotFoundError):
            auth_server._get_apple_client_secret()

    def test_generates_jwt(self, tmp_path, monkeypatch):
        import auth_server

        # Generate a fake EC private key for testing
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization

        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        key_file = tmp_path / "test.p8"
        key_file.write_bytes(pem)

        monkeypatch.setattr(auth_server, "APPLE_PRIVATE_KEY_PATH", str(key_file))
        monkeypatch.setattr(auth_server, "APPLE_TEAM_ID", "TEAMID123")
        monkeypatch.setattr(auth_server, "APPLE_KEY_ID", "KEYID456")
        monkeypatch.setattr(auth_server, "APPLE_SERVICE_ID", "dev.test.auth")

        secret = auth_server._get_apple_client_secret()
        assert isinstance(secret, str)
        assert len(secret) > 50  # JWT should be substantial


# ---------------------------------------------------------------------------
# _fetch_apple_keys
# ---------------------------------------------------------------------------


class TestFetchAppleKeys:
    @pytest.mark.asyncio
    async def test_fetches_and_caches_keys(self, monkeypatch):
        import auth_server

        monkeypatch.setattr(auth_server, "_apple_keys_cache", {})
        monkeypatch.setattr(auth_server, "_apple_keys_fetched", 0)

        fake_keys = {"keys": [{"kid": "abc", "kty": "RSA"}]}

        mock_response = MagicMock()
        mock_response.json.return_value = fake_keys
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("auth_server.httpx.AsyncClient", return_value=mock_client):
            result = await auth_server._fetch_apple_keys()

        assert result == fake_keys
        assert auth_server._apple_keys_cache == fake_keys

    @pytest.mark.asyncio
    async def test_returns_cached_keys_within_ttl(self, monkeypatch):
        import auth_server

        cached = {"keys": [{"kid": "cached"}]}
        monkeypatch.setattr(auth_server, "_apple_keys_cache", cached)
        monkeypatch.setattr(auth_server, "_apple_keys_fetched", time.time())

        result = await auth_server._fetch_apple_keys()
        assert result == cached


# ---------------------------------------------------------------------------
# _verify_apple_id_token
# ---------------------------------------------------------------------------


class TestVerifyAppleIdToken:
    @pytest.mark.asyncio
    async def test_no_matching_key_raises(self):
        import auth_server

        fake_keys = {"keys": [{"kid": "wrong-kid"}]}
        with (
            patch.object(
                auth_server,
                "_fetch_apple_keys",
                new_callable=AsyncMock,
                return_value=fake_keys,
            ),
            patch("auth_server.pyjwt.get_unverified_header", return_value={"kid": "my-kid"}),
        ):
            with pytest.raises(ValueError, match="No matching Apple public key"):
                await auth_server._verify_apple_id_token("fake.jwt.token")

    @pytest.mark.asyncio
    async def test_successful_verification(self):
        import auth_server

        fake_keys = {"keys": [{"kid": "my-kid", "kty": "RSA", "n": "abc", "e": "AQAB"}]}
        mock_pubkey = MagicMock()
        fake_claims = {"sub": "apple-user-123", "email": "test@example.com"}

        with (
            patch.object(
                auth_server,
                "_fetch_apple_keys",
                new_callable=AsyncMock,
                return_value=fake_keys,
            ),
            patch("auth_server.pyjwt.get_unverified_header", return_value={"kid": "my-kid"}),
            patch("auth_server.pyjwt.algorithms.RSAAlgorithm.from_jwk", return_value=mock_pubkey),
            patch("auth_server.pyjwt.decode", return_value=fake_claims),
        ):
            claims = await auth_server._verify_apple_id_token("valid.jwt.token")

        assert claims["sub"] == "apple-user-123"
