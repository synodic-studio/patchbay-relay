"""Additional bridge.py coverage tests for _notify_delivery_failure,
stall detector bot notifications, and cmd_ping edge cases."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

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
        monkeypatch.setattr(bridge, "_active_procs", {})
        monkeypatch.setattr(bridge, "_proc_last_active", {})
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
        bridge._active_procs["100_200"] = proc
        bridge._proc_last_active["100_200"] = time.time() - 10000

        with patch.object(bridge, "_get_proc_cpu", return_value=0.0):
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
        bridge._active_procs["100_200"] = proc
        bridge._proc_last_active["100_200"] = time.time() - 10000

        with patch.object(bridge, "_get_proc_cpu", return_value=0.0):
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
        bridge._active_procs["100"] = proc
        bridge._proc_last_active["100"] = time.time() - 10000

        with patch.object(bridge, "_get_proc_cpu", return_value=0.0):
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
    async def test_cpu_none_continues(self, monkeypatch):
        """When _get_proc_cpu returns None, the process should be skipped."""
        monkeypatch.setattr(bridge, "_bot_instance", None)

        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 555
        bridge._active_procs["100"] = proc
        bridge._proc_last_active["100"] = time.time() - 10000

        with patch.object(bridge, "_get_proc_cpu", return_value=None):
            task = asyncio.create_task(bridge._stall_detector())
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_low_cpu_reading_sets_baseline(self, monkeypatch):
        """First time a process shows low CPU, it should set a baseline
        rather than immediately killing."""
        monkeypatch.setattr(bridge, "_bot_instance", None)
        monkeypatch.setattr(bridge, "STALL_TIMEOUT", 9999)  # very long timeout

        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 444
        bridge._active_procs["100"] = proc
        # Don't pre-set _proc_last_active — first reading

        with patch.object(bridge, "_get_proc_cpu", return_value=0.0):
            task = asyncio.create_task(bridge._stall_detector())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        proc.kill.assert_not_called()
        assert "100" in bridge._proc_last_active


# ---------------------------------------------------------------------------
# cmd_ping with unknown start time
# ---------------------------------------------------------------------------


class TestCmdPingUnknownStart:
    @pytest.fixture(autouse=True)
    def _isolate(self):
        bridge._processing_sessions.clear()
        bridge._session_start_times.clear()
        yield
        bridge._processing_sessions.clear()
        bridge._session_start_times.clear()

    @pytest.mark.asyncio
    async def test_unknown_start_time(self):
        bridge._processing_sessions.add("999_1")
        # No entry in _session_start_times
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        await bridge.cmd_ping(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "999_1" in reply
        assert "unknown" in reply.lower()
