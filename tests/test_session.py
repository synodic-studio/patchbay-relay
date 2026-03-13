"""Tests for session round-trip: save_session_id, get_session_id, clear_session."""

import json
import time

import pytest


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect SESSION_DIR to a temp directory so tests don't touch real sessions."""
    import bridge

    monkeypatch.setattr(bridge, "SESSION_DIR", tmp_path)


@pytest.fixture
def session_fns():
    import bridge

    return bridge.get_session_id, bridge.save_session_id, bridge.clear_session


class TestSessionRoundTrip:
    def test_save_then_load(self, session_fns):
        get, save, _ = session_fns
        save("chat_42", "sess-abc")
        assert get("chat_42") == "sess-abc"

    def test_load_nonexistent_returns_none(self, session_fns):
        get, _, _ = session_fns
        assert get("does_not_exist") is None

    def test_clear_removes_session(self, session_fns):
        get, save, clear = session_fns
        save("chat_99", "sess-xyz")
        clear("chat_99")
        assert get("chat_99") is None

    def test_clear_nonexistent_is_noop(self, session_fns):
        _, _, clear = session_fns
        clear("never_existed")  # should not raise

    def test_overwrite_session(self, session_fns):
        get, save, _ = session_fns
        save("chat_1", "first")
        save("chat_1", "second")
        assert get("chat_1") == "second"

    def test_independent_keys(self, session_fns):
        get, save, clear = session_fns
        save("a", "sess-a")
        save("b", "sess-b")
        clear("a")
        assert get("a") is None
        assert get("b") == "sess-b"


class TestSessionExpiry:
    def test_expired_session_returns_none(self, tmp_path, session_fns, monkeypatch):

        get, _, _ = session_fns
        # Write a session file that expired long ago
        session_file = tmp_path / "old_chat.json"
        session_file.write_text(
            json.dumps({"session_id": "old-sess", "last_active": time.time() - 999999})
        )
        assert get("old_chat") is None
        # Expired file should be cleaned up
        assert not session_file.exists()

    def test_fresh_session_survives(self, session_fns):
        get, save, _ = session_fns
        save("fresh", "sess-fresh")
        assert get("fresh") == "sess-fresh"


class TestSessionCorruptFile:
    def test_malformed_json_returns_none(self, tmp_path, session_fns):
        get, _, _ = session_fns
        bad_file = tmp_path / "corrupt.json"
        bad_file.write_text("not valid json{{{")
        assert get("corrupt") is None
        # Corrupt file should be cleaned up
        assert not bad_file.exists()
