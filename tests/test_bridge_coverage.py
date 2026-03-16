"""Additional bridge.py coverage tests for handle_message edge cases,
_auth_notify, _notify_delivery_failure, cmd_remote_control,
cmd_restart with remote proc, quota handoff, and stall detector bot notifications."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge
import stargate.sessions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_update(
    chat_id=1, thread_id=None, user_id=42, text="hello"
):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.reply_text = AsyncMock()
    return update


def _make_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.args = []
    return ctx


# ---------------------------------------------------------------------------
# handle_message: auth and unauthorized paths
# ---------------------------------------------------------------------------


class TestHandleMessageAuth:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        monkeypatch.setattr(stargate.sessions, "PENDING_DIR", tmp_path / "pending")
        (tmp_path / "pending").mkdir()
        monkeypatch.setattr(bridge, "PENDING_DIR", tmp_path / "pending")
        monkeypatch.setattr(bridge, "SEND_RETRY_BASE_DELAY", 0.01)
        bridge._shutting_down = False
        bridge._processing_sessions.clear()
        bridge._queued_messages.clear()
        bridge._session_start_times.clear()

    @pytest.mark.asyncio
    async def test_unauthorized_user_silently_ignored(self, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", {9999})
        update = _make_update(user_id=42)
        ctx = _make_context()
        await bridge.handle_message(update, ctx)
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_auth_required_sends_link(self, monkeypatch, tmp_auth_state):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", True)
        update = _make_update(user_id=42)
        ctx = _make_context()
        await bridge.handle_message(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "Authentication required" in reply or "auth" in reply.lower()

    @pytest.mark.asyncio
    async def test_empty_text_returns_early(self, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", False)
        update = _make_update()
        update.message.text = None
        ctx = _make_context()
        await bridge.handle_message(update, ctx)
        # Should not crash and should not send a response
        ctx.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_quota_handoff_success(self, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", False)
        update = _make_update(text="do something expensive")
        ctx = _make_context()

        with (
            patch.object(
                bridge,
                "run_claude",
                return_value=bridge.QUOTA_HIT_PREFIX + "do something expensive",
            ),
            patch.object(bridge, "_handoff_to_forge", return_value=True),
        ):
            await bridge.handle_message(update, ctx)

        ctx.bot.send_message.assert_called()
        sent_text = ctx.bot.send_message.call_args.kwargs.get(
            "text", ctx.bot.send_message.call_args[1].get("text", "")
        )
        assert "Forge" in sent_text

    @pytest.mark.asyncio
    async def test_quota_handoff_failure(self, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", False)
        update = _make_update(text="expensive task")
        ctx = _make_context()

        with (
            patch.object(
                bridge,
                "run_claude",
                return_value=bridge.QUOTA_HIT_PREFIX + "expensive task",
            ),
            patch.object(bridge, "_handoff_to_forge", return_value=False),
        ):
            await bridge.handle_message(update, ctx)

        ctx.bot.send_message.assert_called()
        sent_text = ctx.bot.send_message.call_args.kwargs.get(
            "text", ctx.bot.send_message.call_args[1].get("text", "")
        )
        assert "failed" in sent_text.lower()

    @pytest.mark.asyncio
    async def test_send_response_failure_notifies(self, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", False)
        update = _make_update(text="hello")
        ctx = _make_context()

        with patch.object(bridge, "run_claude", return_value="good response"):
            # Make all sends fail, then succeed on notification
            ctx.bot.send_message = AsyncMock(
                side_effect=[Exception("fail")] * 3 + [None]
            )
            await bridge.handle_message(update, ctx)

        assert ctx.bot.send_message.call_count >= 3

    @pytest.mark.asyncio
    async def test_queued_batch_claude_error(self, monkeypatch):
        """Queue drain where the batch invocation raises an error."""
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", False)

        update = _make_update(chat_id=5, thread_id=6, text="initial")
        ctx = _make_context()

        call_count = [0]

        def mock_run_claude(msg, key):
            call_count[0] += 1
            if call_count[0] == 1:
                # During first call, queue a message
                bridge._queued_messages.setdefault(key, []).append("queued msg")
                return "first response"
            else:
                raise RuntimeError("batch error")

        with patch.object(bridge, "run_claude", side_effect=mock_run_claude):
            await bridge.handle_message(update, ctx)

        # Both the initial and the error response should have been sent
        assert ctx.bot.send_message.call_count >= 2

    @pytest.mark.asyncio
    async def test_queued_batch_send_failure(self, monkeypatch):
        """Queue drain where sending the batch response fails."""
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", False)

        update = _make_update(chat_id=5, thread_id=6, text="initial")
        ctx = _make_context()

        call_count = [0]

        def mock_run_claude(msg, key):
            call_count[0] += 1
            if call_count[0] == 1:
                bridge._queued_messages.setdefault(key, []).append("queued msg")
                return "first response"
            return "batch response"

        send_count = [0]

        async def mock_send(*args, **kwargs):
            send_count[0] += 1
            if send_count[0] >= 2:
                # Fail on second send (the batch response) multiple times, then succeed
                if send_count[0] <= 4:
                    raise Exception("telegram fail")

        ctx.bot.send_message = AsyncMock(side_effect=mock_send)

        with patch.object(bridge, "run_claude", side_effect=mock_run_claude):
            await bridge.handle_message(update, ctx)


# ---------------------------------------------------------------------------
# _auth_notify
# ---------------------------------------------------------------------------


class TestAuthNotify:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        bridge._bot_instance = None

    @pytest.mark.asyncio
    async def test_no_allowed_users_does_nothing(self, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(bridge, "_bot_instance", bot)
        await bridge._auth_notify("authenticated", 42, "test")
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_to_all_admins(self, monkeypatch):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(bridge, "_bot_instance", bot)
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", {100, 200})
        await bridge._auth_notify("authenticated", 42, "IP: 1.2.3.4")
        assert bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_known_event_types(self, monkeypatch):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(bridge, "_bot_instance", bot)
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", {100})

        for event_type, label in [
            ("authenticated", "NEW AUTH"),
            ("denied", "ACCESS DENIED"),
            ("ip_changed", "IP CHANGE"),
            ("locked", "SESSION LOCKED"),
            ("expired", "SESSION EXPIRED"),
        ]:
            bot.send_message.reset_mock()
            await bridge._auth_notify(event_type, 42, "details")
            msg = bot.send_message.call_args.kwargs["text"]
            assert label in msg

    @pytest.mark.asyncio
    async def test_unknown_event_type_uppercased(self, monkeypatch):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(bridge, "_bot_instance", bot)
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", {100})
        await bridge._auth_notify("custom_event", 42)
        msg = bot.send_message.call_args.kwargs["text"]
        assert "CUSTOM_EVENT" in msg

    @pytest.mark.asyncio
    async def test_send_failure_is_suppressed(self, monkeypatch):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=Exception("network error"))
        monkeypatch.setattr(bridge, "_bot_instance", bot)
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", {100})
        # Should not raise
        await bridge._auth_notify("authenticated", 42)


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
# cmd_remote_control
# ---------------------------------------------------------------------------


class TestCmdRemoteControl:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "_remote_proc", None)
        monkeypatch.setattr(bridge, "_remote_proc_key", None)
        import stargate.projects

        monkeypatch.setattr(
            stargate.projects, "CHAT_PROJECTS_FILE", tmp_path / "cp.json"
        )
        self._tmp = tmp_path

    @pytest.mark.asyncio
    async def test_unauthorized_user_ignored(self, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", {9999})
        update = _make_update(user_id=42, text="/remote_control")
        ctx = _make_context()
        await bridge.cmd_remote_control(update, ctx)
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_no_process(self):
        update = _make_update(text="/remote_control stop")
        ctx = _make_context()
        await bridge.cmd_remote_control(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "No remote-control" in reply

    @pytest.mark.asyncio
    async def test_stop_running_process(self, monkeypatch):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = None
        proc.pid = 1234
        monkeypatch.setattr(bridge, "_remote_proc", proc)
        monkeypatch.setattr(bridge, "_remote_proc_key", "1_2")

        update = _make_update(chat_id=1, thread_id=2, text="/remote_control stop")
        ctx = _make_context()
        await bridge.cmd_remote_control(update, ctx)

        proc.terminate.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "stopped" in reply.lower()
        assert bridge._remote_proc is None

    @pytest.mark.asyncio
    async def test_start_launches_process(self, monkeypatch):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # exits immediately
        mock_proc.returncode = 0
        mock_proc.pid = 5678
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read.return_value = "Session URL: https://example.com/rc\n"
        mock_proc.stdout.readline.return_value = ""

        with patch("bridge.subprocess.Popen", return_value=mock_proc):
            update = _make_update(chat_id=10, thread_id=20, text="/remote_control")
            ctx = _make_context()
            await bridge.cmd_remote_control(update, ctx)

        # Should report the process exited
        reply = update.message.reply_text.call_args[0][0]
        assert "exited" in reply.lower() or "Remote control" in reply

    @pytest.mark.asyncio
    async def test_replaces_existing_process(self, monkeypatch):
        old_proc = MagicMock()
        old_proc.poll.return_value = None
        old_proc.wait.return_value = None
        old_proc.pid = 1111
        monkeypatch.setattr(bridge, "_remote_proc", old_proc)
        monkeypatch.setattr(bridge, "_remote_proc_key", "old")

        new_proc = MagicMock()
        new_proc.poll.return_value = 0
        new_proc.returncode = 0
        new_proc.pid = 2222
        new_proc.stdout = MagicMock()
        new_proc.stdout.read.return_value = ""
        new_proc.stdout.readline.return_value = ""

        with patch("bridge.subprocess.Popen", return_value=new_proc):
            update = _make_update(text="/remote_control")
            ctx = _make_context()
            await bridge.cmd_remote_control(update, ctx)

        old_proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_restart with remote proc
# ---------------------------------------------------------------------------


class TestCmdRestartRemoteProc:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "RESTART_NOTIFY_FILE", tmp_path / "restart.json")
        monkeypatch.setattr(bridge, "_active_procs", {})

    @pytest.mark.asyncio
    async def test_terminates_remote_proc_on_restart(self, monkeypatch):
        remote = MagicMock()
        remote.poll.return_value = None
        remote.pid = 3333
        monkeypatch.setattr(bridge, "_remote_proc", remote)

        update = _make_update()
        ctx = _make_context()
        with patch("os._exit"):
            await bridge.cmd_restart(update, ctx)

        remote.terminate.assert_called_once()


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
        update = _make_update()
        ctx = _make_context()
        await bridge.cmd_ping(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "999_1" in reply
        assert "unknown" in reply.lower()


# ---------------------------------------------------------------------------
# cmd_lock unauthorized user
# ---------------------------------------------------------------------------


class TestCmdLockUnauthorized:
    @pytest.mark.asyncio
    async def test_unauthorized_user_silently_ignored(self, monkeypatch, tmp_auth_state):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", {9999})
        update = _make_update(user_id=42)
        ctx = _make_context()
        await bridge.cmd_lock(update, ctx)
        update.message.reply_text.assert_not_called()


# ---------------------------------------------------------------------------
# _check_auth and _send_auth_link edge cases
# ---------------------------------------------------------------------------


class TestCheckAuthAndSendLink:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_auth_state, monkeypatch):
        pass

    @pytest.mark.asyncio
    async def test_check_auth_no_auth_required(self, monkeypatch):
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", False)
        update = _make_update(user_id=42)
        result = await bridge._check_auth(update)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_auth_authenticated_user(self, monkeypatch):
        import auth

        monkeypatch.setattr(bridge, "AUTH_REQUIRED", True)
        auth.create_session(42, "sub", "1.1.1.1")
        update = _make_update(user_id=42)
        result = await bridge._check_auth(update)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_auth_unauthenticated(self, monkeypatch):
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", True)
        update = _make_update(user_id=42)
        result = await bridge._check_auth(update)
        assert result is False

    @pytest.mark.asyncio
    async def test_send_auth_link_rate_limited(self, monkeypatch):
        import auth

        for _ in range(5):
            auth.record_failed_attempt(42)
        update = _make_update(user_id=42)
        await bridge._send_auth_link(update)
        reply = update.message.reply_text.call_args[0][0]
        assert "Too many" in reply

    @pytest.mark.asyncio
    async def test_send_auth_link_normal(self, monkeypatch):
        update = _make_update(user_id=42)
        await bridge._send_auth_link(update)
        reply = update.message.reply_text.call_args[0][0]
        assert "Authentication required" in reply
        assert "auth.kj6.dev" in reply
