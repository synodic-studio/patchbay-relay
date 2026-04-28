"""Additional bridge.py coverage tests for _notify_delivery_failure,
stall detector bot notifications, and cmd_ping edge cases."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import bridge


# ---------------------------------------------------------------------------
# _notify_delivery_failure
# ---------------------------------------------------------------------------


class TestNotifyDeliveryFailure:
    @pytest.mark.asyncio
    async def test_sends_failure_notification(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._notify_delivery_failure(bot, 123, 456, "test_key")
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 123
        assert call_kwargs["message_thread_id"] == 456
        assert "could not be delivered" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_notification_failure_suppressed(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=Exception("double failure"))
        # Should not raise
        await bridge._notify_delivery_failure(bot, 123, None, "test_key")

    @pytest.mark.asyncio
    async def test_no_thread_id(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._notify_delivery_failure(bot, 123, None, "test")
        call_kwargs = bot.send_message.call_args.kwargs
        assert "message_thread_id" not in call_kwargs


# ---------------------------------------------------------------------------
# stall detector with bot notifications
# ---------------------------------------------------------------------------


class TestStallDetectorNotify:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setattr(bridge, "_sessions", {})
        monkeypatch.setattr(bridge, "STALL_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(bridge, "STALL_TIMEOUT", 0.01)

    @pytest.mark.asyncio
    async def test_notifies_on_stall_kill(self, monkeypatch):
        """When a process is killed for stalling and _bot_instance is set,
        it should send a notification."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(bridge, "_bot_instance", bot)

        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 777
        bridge._get_session_state("100_200").proc = proc
        bridge._get_session_state("100_200").last_event_at = time.time() - 10000

        task = asyncio.create_task(bridge._stall_detector())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        proc.kill.assert_called()
        bot.send_message.assert_called()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 100
        assert call_kwargs["message_thread_id"] == 200

    @pytest.mark.asyncio
    async def test_stall_notify_failure_suppressed(self, monkeypatch):
        """If notification fails, the stall detector should not crash."""
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=Exception("network"))
        monkeypatch.setattr(bridge, "_bot_instance", bot)

        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 888
        bridge._get_session_state("100_200").proc = proc
        bridge._get_session_state("100_200").last_event_at = time.time() - 10000

        task = asyncio.create_task(bridge._stall_detector())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Process should still have been killed even if notification failed
        proc.kill.assert_called()

    @pytest.mark.asyncio
    async def test_stall_key_no_thread_id(self, monkeypatch):
        """Session key without underscore (no thread_id) should work."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(bridge, "_bot_instance", bot)

        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 999
        bridge._get_session_state("100").proc = proc
        bridge._get_session_state("100").last_event_at = time.time() - 10000

        task = asyncio.create_task(bridge._stall_detector())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        proc.kill.assert_called()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 100

    @pytest.mark.asyncio
    async def test_first_seen_proc_gets_baseline_not_killed(self, monkeypatch):
        """A proc that's tracked for the first time (last_event_at is None)
        gets a baseline timestamp rather than being killed immediately."""
        monkeypatch.setattr(bridge, "_bot_instance", None)
        monkeypatch.setattr(bridge, "STALL_TIMEOUT", 9999)  # very long timeout

        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 444
        bridge._get_session_state("100").proc = proc
        bridge._sessions["100"].last_event_at = None  # first reading

        task = asyncio.create_task(bridge._stall_detector())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        proc.kill.assert_not_called()
        assert bridge._sessions["100"].last_event_at is not None


# ---------------------------------------------------------------------------
# cmd_ping with unknown start time
# ---------------------------------------------------------------------------


class TestCmdPingUnknownStart:
    @pytest.fixture(autouse=True)
    def _isolate(self):
        bridge._sessions.clear()
        yield
        bridge._sessions.clear()

    @pytest.mark.asyncio
    async def test_unknown_start_time(self):
        bridge._get_session_state("999_1").processing = True
        # No entry in _session_start_times
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        await bridge.cmd_ping(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "999_1" in reply
        assert "unknown" in reply.lower()


# ---------------------------------------------------------------------------
# _session_display_label resolution
# ---------------------------------------------------------------------------


class TestSessionDisplayLabel:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        projects_file = tmp_path / "chat_projects.json"
        monkeypatch.setattr(
            "patchbay.projects.CHAT_PROJECTS_FILE", projects_file
        )
        yield

    def test_title_wins_over_project_and_agent(self):
        from patchbay.projects import _save_chat_projects

        _save_chat_projects(
            {"k": {"path": "Fanta", "agent": "ernest", "title": "Ernest 🟢"}}
        )
        assert bridge._session_display_label("k") == "Ernest 🟢"

    def test_project_with_agent_falls_back_to_arrow_form(self):
        from patchbay.projects import _save_chat_projects

        _save_chat_projects({"k": {"path": "Fanta", "agent": "iron-temple"}})
        assert bridge._session_display_label("k") == "Fanta › iron-temple"

    def test_project_only(self):
        from patchbay.projects import _save_chat_projects

        _save_chat_projects({"k": "patchbay-relay"})
        assert bridge._session_display_label("k") == "patchbay-relay"

    def test_agent_only(self):
        from patchbay.projects import _save_chat_projects

        _save_chat_projects({"k": {"agent": "ernest"}})
        assert bridge._session_display_label("k") == "ernest"

    def test_unknown_key_returns_session_key(self):
        assert bridge._session_display_label("12345_67") == "12345_67"


# ---------------------------------------------------------------------------
# handle_forum_topic_event caches topic names
# ---------------------------------------------------------------------------


class TestHandleForumTopicEvent:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        projects_file = tmp_path / "chat_projects.json"
        monkeypatch.setattr(
            "patchbay.projects.CHAT_PROJECTS_FILE", projects_file
        )
        yield

    def _make_update(self, chat_id, thread_id, *, created=None, edited=None):
        msg = MagicMock()
        msg.message_thread_id = thread_id
        msg.forum_topic_created = created
        msg.forum_topic_edited = edited
        update = MagicMock()
        update.effective_message = msg
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
        return update

    @pytest.mark.asyncio
    async def test_cache_on_create(self):
        from patchbay.projects import get_chat_title

        created = MagicMock()
        created.name = "patchbay-relay"
        update = self._make_update(-100, 30, created=created)
        await bridge.handle_forum_topic_event(update, MagicMock())
        assert get_chat_title("-100_30") == "patchbay-relay"

    @pytest.mark.asyncio
    async def test_cache_on_edit_overwrites(self):
        from patchbay.projects import get_chat_title, set_chat_title

        set_chat_title("-100_30", "old name")
        edited = MagicMock()
        edited.name = "new name"
        update = self._make_update(-100, 30, edited=edited)
        await bridge.handle_forum_topic_event(update, MagicMock())
        assert get_chat_title("-100_30") == "new name"

    @pytest.mark.asyncio
    async def test_no_thread_id_is_skipped(self):
        from patchbay.projects import _load_chat_projects

        created = MagicMock()
        created.name = "irrelevant"
        update = self._make_update(-100, None, created=created)
        await bridge.handle_forum_topic_event(update, MagicMock())
        assert _load_chat_projects() == {}
