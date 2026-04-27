"""Tests for pending message file write/clear lifecycle."""

import json
import os
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

# Add parent directory so we can import bridge
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge

# bridge.PENDING_DIR is read fresh in each test so conftest's autouse path
# isolation (which monkeypatches the attribute to a tmp dir) is picked up.
# Don't capture it as a module-level constant — that snapshots the real
# production path before any fixture runs.


class TestSavePending:
    def test_creates_json_file(self):
        pid = bridge.save_pending(123, 456, "hello", "123:456")
        path = bridge.PENDING_DIR / f"{pid}.json"
        assert path.exists()

    def test_file_contains_correct_fields(self):
        pid = bridge.save_pending(123, 456, "hello world", "123:456")
        data = json.loads((bridge.PENDING_DIR / f"{pid}.json").read_text())
        assert data["chat_id"] == 123
        assert data["thread_id"] == 456
        assert data["text"] == "hello world"
        assert data["session_key"] == "123:456"
        assert isinstance(data["timestamp"], float)

    def test_none_thread_id(self):
        pid = bridge.save_pending(123, None, "hi", "123:None")
        data = json.loads((bridge.PENDING_DIR / f"{pid}.json").read_text())
        assert data["thread_id"] is None

    def test_returns_unique_ids(self):
        ids = {bridge.save_pending(1, None, "m", "1:None") for _ in range(20)}
        assert len(ids) == 20


class TestClearPending:
    def test_removes_file(self):
        pid = bridge.save_pending(1, None, "text", "1:None")
        path = bridge.PENDING_DIR / f"{pid}.json"
        assert path.exists()
        bridge.clear_pending(pid)
        assert not path.exists()

    def test_no_error_on_missing_file(self):
        bridge.clear_pending("nonexistent_id_abc")  # should not raise


class TestReplayPending:
    @pytest.mark.asyncio
    async def test_skips_malformed_json(self):
        (bridge.PENDING_DIR / "bad.json").write_text("not json{{{")
        bot = AsyncMock()
        await bridge.replay_pending(bot)
        assert not (bridge.PENDING_DIR / "bad.json").exists()

    @pytest.mark.asyncio
    async def test_skips_missing_required_keys(self):
        (bridge.PENDING_DIR / "incomplete.json").write_text(json.dumps({"chat_id": 1, "text": "hi"}))
        bot = AsyncMock()
        await bridge.replay_pending(bot)
        assert not (bridge.PENDING_DIR / "incomplete.json").exists()

    @pytest.mark.asyncio
    async def test_skips_expired_messages(self):
        expired_data = {
            "chat_id": 1,
            "thread_id": None,
            "text": "old",
            "session_key": "1:None",
            "timestamp": time.time() - bridge.SESSION_EXPIRY - 100,
        }
        (bridge.PENDING_DIR / "expired.json").write_text(json.dumps(expired_data))
        bot = AsyncMock()
        await bridge.replay_pending(bot)
        assert not (bridge.PENDING_DIR / "expired.json").exists()

    @pytest.mark.asyncio
    async def test_replays_valid_message(self):
        pid = bridge.save_pending(99, 10, "replay me", "99:10")
        bot = AsyncMock()

        with patch.object(bridge, "run_claude", return_value="response text"):
            await bridge.replay_pending(bot)

        assert not (bridge.PENDING_DIR / f"{pid}.json").exists()
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args
        text_sent = call_kwargs.kwargs.get("text", call_kwargs.args[0] if call_kwargs.args else "")
        assert "Recovered after bridge restart" in text_sent

    @pytest.mark.asyncio
    async def test_noop_when_no_pending_files(self):
        bot = AsyncMock()
        await bridge.replay_pending(bot)
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_present_with_bumped_attempts_during_processing(self):
        """File stays during run_claude (so crashes don't lose it), but with
        attempts incremented to 1. After successful send, file is deleted."""
        pid = bridge.save_pending(1, None, "msg", "1_None")
        path = bridge.PENDING_DIR / f"{pid}.json"

        observed = []

        def fake_run_claude(text, key):
            data = json.loads(path.read_text())
            observed.append((path.exists(), data.get("attempts")))
            return "ok"

        bot = AsyncMock()
        with patch.object(bridge, "run_claude", side_effect=fake_run_claude):
            await bridge.replay_pending(bot)

        assert observed == [(True, 1)]
        assert not path.exists()  # deleted on successful delivery
