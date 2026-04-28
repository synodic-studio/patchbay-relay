"""Tests for bridge.post_init — bot command registration, restart notification,
and replay_pending with live execution."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge
import patchbay.projects
import patchbay.sessions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_mock(bot=None):
    """Build a mock Application with a bot attached."""
    if bot is None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.set_my_commands = AsyncMock()
        bot.delete_my_commands = AsyncMock()
    app_mock = MagicMock()
    app_mock.bot = bot
    return app_mock


# ---------------------------------------------------------------------------
# post_init
# ---------------------------------------------------------------------------


class TestPostInit:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(patchbay.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        monkeypatch.setattr(patchbay.sessions, "PENDING_DIR", tmp_path / "pending")
        (tmp_path / "pending").mkdir()
        monkeypatch.setattr(bridge, "PENDING_DIR", tmp_path / "pending")
        monkeypatch.setattr(bridge, "RESTART_NOTIFY_FILE", tmp_path / "restart.json")
        monkeypatch.setattr(bridge, "STALL_POLL_INTERVAL", 999)
        monkeypatch.setattr(bridge, "STALL_TIMEOUT", 999)
        # Prevent real chat_projects loading
        monkeypatch.setattr(patchbay.projects, "CHAT_PROJECTS_FILE", tmp_path / "cp.json")
        bridge._bot_instance = None
        self._tmp = tmp_path

    @pytest.mark.asyncio
    async def test_sets_bot_instance(self):
        app_mock = _make_app_mock()
        with patch("bridge._stall_detector", new_callable=AsyncMock):
            await bridge.post_init(app_mock)
        assert bridge._bot_instance is app_mock.bot

    @pytest.mark.asyncio
    async def test_registers_commands(self):
        app_mock = _make_app_mock()
        with patch("bridge._stall_detector", new_callable=AsyncMock):
            await bridge.post_init(app_mock)
        app_mock.bot.set_my_commands.assert_called_once()
        commands = app_mock.bot.set_my_commands.call_args[0][0]
        cmd_names = [c.command for c in commands]
        assert "clearnew" in cmd_names
        assert "ping" in cmd_names
        assert "kill" in cmd_names

    @pytest.mark.asyncio
    async def test_deletes_old_commands(self):
        app_mock = _make_app_mock()
        with patch("bridge._stall_detector", new_callable=AsyncMock):
            await bridge.post_init(app_mock)
        # Should have called delete_my_commands for generic scopes
        assert app_mock.bot.delete_my_commands.call_count >= 4

    @pytest.mark.asyncio
    async def test_restart_notification_sent(self):
        notify_file = self._tmp / "restart.json"
        notify_file.write_text(json.dumps({"chat_id": 123, "thread_id": 456}))

        app_mock = _make_app_mock()
        with patch("bridge._stall_detector", new_callable=AsyncMock):
            await bridge.post_init(app_mock)

        app_mock.bot.send_message.assert_called()
        call_kwargs = app_mock.bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 123
        assert call_kwargs["message_thread_id"] == 456
        # File should be cleaned up
        assert not notify_file.exists()

    @pytest.mark.asyncio
    async def test_restart_notification_no_thread(self):
        notify_file = self._tmp / "restart.json"
        notify_file.write_text(json.dumps({"chat_id": 999}))

        app_mock = _make_app_mock()
        with patch("bridge._stall_detector", new_callable=AsyncMock):
            await bridge.post_init(app_mock)

        # send_message should be called but without message_thread_id
        app_mock.bot.send_message.assert_called()
        assert not notify_file.exists()

    @pytest.mark.asyncio
    async def test_no_restart_file_is_fine(self):
        """post_init should not fail when restart_notify.json doesn't exist."""
        app_mock = _make_app_mock()
        with patch("bridge._stall_detector", new_callable=AsyncMock):
            await bridge.post_init(app_mock)
        # Just verify it completes without error

    @pytest.mark.asyncio
    async def test_corrupt_restart_file_handled(self):
        notify_file = self._tmp / "restart.json"
        notify_file.write_text("not json")

        app_mock = _make_app_mock()
        with patch("bridge._stall_detector", new_callable=AsyncMock):
            await bridge.post_init(app_mock)
        # Should not raise; file should be cleaned up
        assert not notify_file.exists()

    @pytest.mark.asyncio
    async def test_starts_stall_detector(self):
        app_mock = _make_app_mock()
        import asyncio

        created_tasks = []
        orig_create_task = asyncio.create_task

        def capture_create_task(coro, *args, **kwargs):
            task = orig_create_task(coro, *args, **kwargs)
            created_tasks.append(task)
            return task

        with patch("asyncio.create_task", side_effect=capture_create_task):
            await bridge.post_init(app_mock)

        # At least one task should have been created (stall detector)
        assert len(created_tasks) >= 1
        # Clean up
        for t in created_tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# replay_pending with live execution
# ---------------------------------------------------------------------------


class TestReplayPendingLive:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(patchbay.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        pending = tmp_path / "pending"
        pending.mkdir()
        monkeypatch.setattr(patchbay.sessions, "PENDING_DIR", pending)
        monkeypatch.setattr(bridge, "PENDING_DIR", pending)
        self._pending = pending

    @pytest.mark.asyncio
    async def test_replays_pending_message(self):
        import time

        pending_data = {
            "chat_id": 100,
            "thread_id": 200,
            "text": "Hello from pending",
            "session_key": "100_200",
            "timestamp": time.time(),
        }
        (self._pending / "abc123.json").write_text(json.dumps(pending_data))

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_chat_action = AsyncMock()

        with patch.object(bridge, "run_claude", return_value="Replayed response"):
            await bridge.replay_pending(bot)

        bot.send_message.assert_called()
        sent_text = bot.send_message.call_args.kwargs.get("text", bot.send_message.call_args[1].get("text", ""))
        assert "Recovered" in sent_text
        assert "Replayed response" in sent_text
        # Pending file should be deleted
        assert not (self._pending / "abc123.json").exists()

    @pytest.mark.asyncio
    async def test_replay_claude_error_does_not_notify_on_first_attempt(self):
        """run_claude error: leave the bumped pending file, do not send.
        The next bridge restart will retry; only after PENDING_MAX_ATTEMPTS
        do we archive and notify."""
        import time

        pending_data = {
            "chat_id": 100,
            "thread_id": None,
            "text": "test",
            "session_key": "100",
            "timestamp": time.time(),
            "attempts": 0,
        }
        path = self._pending / "def456.json"
        path.write_text(json.dumps(pending_data))

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_chat_action = AsyncMock()

        with patch.object(bridge, "run_claude", side_effect=RuntimeError("crash")):
            await bridge.replay_pending(bot)

        bot.send_message.assert_not_called()
        # File survived with bumped attempt counter so the next bridge run retries.
        assert path.exists()
        assert json.loads(path.read_text())["attempts"] == 1

    @pytest.mark.asyncio
    async def test_replay_gives_up_after_max_attempts(self):
        """After PENDING_MAX_ATTEMPTS failed retries, archive and notify."""
        import time

        pending_data = {
            "chat_id": 100,
            "thread_id": 5,
            "text": "stubborn message",
            "session_key": "100_5",
            "timestamp": time.time(),
            "attempts": bridge.PENDING_MAX_ATTEMPTS,  # already at the cap
        }
        path = self._pending / "ghi789.json"
        path.write_text(json.dumps(pending_data))

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_chat_action = AsyncMock()

        with patch.object(bridge, "run_claude", side_effect=RuntimeError("crash")):
            await bridge.replay_pending(bot)

        # Original file moved to failed/, gave-up message sent
        assert not path.exists()
        assert (self._pending / "failed" / "ghi789.json").exists()
        bot.send_message.assert_called_once()
        sent_text = bot.send_message.call_args.kwargs.get("text", "")
        assert "giving up" in sent_text
        assert "stubborn message" in sent_text

    @pytest.mark.asyncio
    async def test_replay_no_thread_id(self):
        import time

        pending_data = {
            "chat_id": 100,
            "text": "no thread",
            "session_key": "100",
            "timestamp": time.time(),
        }
        (self._pending / "nothread.json").write_text(json.dumps(pending_data))

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_chat_action = AsyncMock()

        with patch.object(bridge, "run_claude", return_value="ok"):
            await bridge.replay_pending(bot)

        call_kwargs = bot.send_message.call_args.kwargs
        assert "message_thread_id" not in call_kwargs
