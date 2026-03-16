"""Tests for Telegram helper functions in bridge.py:
_send_response, keep_typing, _check_auth, _send_auth_link, _notify_delivery_failure.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# _send_response
# ---------------------------------------------------------------------------


class TestSendResponse:
    """_send_response chunking, retry, and thread_id behavior."""

    @pytest.fixture(autouse=True)
    def _fast_retries(self, monkeypatch):
        import bridge

        monkeypatch.setattr(bridge, "SEND_RETRY_BASE_DELAY", 0.0)
        monkeypatch.setattr(bridge, "SEND_RETRY_ATTEMPTS", 3)

    # -- chunking -----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_empty_response_sends_nothing(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._send_response(bot, 111, None, "")
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_exactly_4096_chars_single_chunk(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        text = "A" * 4096
        await bridge._send_response(bot, 111, None, text)
        bot.send_message.assert_called_once()
        assert bot.send_message.call_args.kwargs["text"] == text

    @pytest.mark.asyncio
    async def test_4097_chars_two_chunks(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        text = "B" * 4097
        await bridge._send_response(bot, 111, None, text)
        assert bot.send_message.call_count == 2
        first_text = bot.send_message.call_args_list[0].kwargs["text"]
        second_text = bot.send_message.call_args_list[1].kwargs["text"]
        assert len(first_text) == 4096
        assert len(second_text) == 1
        assert first_text + second_text == text

    @pytest.mark.asyncio
    async def test_8192_chars_two_full_chunks(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        text = "C" * 8192
        await bridge._send_response(bot, 111, None, text)
        assert bot.send_message.call_count == 2
        for call in bot.send_message.call_args_list:
            assert len(call.kwargs["text"]) == 4096

    @pytest.mark.asyncio
    async def test_very_long_response_five_chunks(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        # 4096 * 4 + 1 = 16385 -> 5 chunks
        text = "D" * (4096 * 4 + 1)
        await bridge._send_response(bot, 111, None, text)
        assert bot.send_message.call_count == 5
        reassembled = "".join(
            call.kwargs["text"] for call in bot.send_message.call_args_list
        )
        assert reassembled == text

    # -- thread_id ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_thread_id_omits_kwarg(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._send_response(bot, 111, None, "hello")
        kwargs = bot.send_message.call_args.kwargs
        assert "message_thread_id" not in kwargs

    @pytest.mark.asyncio
    async def test_thread_id_passed_through(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._send_response(bot, 111, 999, "hello")
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs["message_thread_id"] == 999

    @pytest.mark.asyncio
    async def test_thread_id_on_every_chunk(self):
        """When message is split, every chunk carries the thread_id."""
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        text = "E" * (4096 + 100)
        await bridge._send_response(bot, 111, 42, text)
        assert bot.send_message.call_count == 2
        for call in bot.send_message.call_args_list:
            assert call.kwargs["message_thread_id"] == 42


# ---------------------------------------------------------------------------
# keep_typing
# ---------------------------------------------------------------------------


class TestKeepTyping:
    """keep_typing sends chat actions and respects the stop event."""

    @pytest.fixture(autouse=True)
    def _fast_typing(self, monkeypatch):
        import bridge

        monkeypatch.setattr(bridge, "TYPING_INTERVAL", 0.01)

    @pytest.mark.asyncio
    async def test_stops_immediately_when_event_set(self):
        import bridge

        bot = MagicMock()
        bot.send_chat_action = AsyncMock()
        stop = asyncio.Event()
        stop.set()
        await bridge.keep_typing(111, None, stop, bot)
        # It may send one action before checking the event,
        # but it must not loop forever.
        assert bot.send_chat_action.call_count <= 1

    @pytest.mark.asyncio
    async def test_sends_chat_action_with_thread_id(self):
        import bridge

        bot = MagicMock()
        bot.send_chat_action = AsyncMock()
        stop = asyncio.Event()

        async def _set_after_brief():
            await asyncio.sleep(0.03)
            stop.set()

        asyncio.create_task(_set_after_brief())
        await bridge.keep_typing(222, 55, stop, bot)
        assert bot.send_chat_action.call_count >= 1
        for call in bot.send_chat_action.call_args_list:
            assert call.kwargs["message_thread_id"] == 55
            assert call.kwargs["chat_id"] == 222
            assert call.kwargs["action"] == "typing"

    @pytest.mark.asyncio
    async def test_handles_send_chat_action_exception(self):
        """Exceptions from send_chat_action are swallowed; loop continues."""
        import bridge

        bot = MagicMock()
        bot.send_chat_action = AsyncMock(side_effect=Exception("network blip"))
        stop = asyncio.Event()

        async def _set_after_brief():
            await asyncio.sleep(0.03)
            stop.set()

        asyncio.create_task(_set_after_brief())
        # Should not raise
        await bridge.keep_typing(333, None, stop, bot)
        # It tried at least once
        assert bot.send_chat_action.call_count >= 1


# ---------------------------------------------------------------------------
# _check_auth
# ---------------------------------------------------------------------------


class TestCheckAuth:
    """_check_auth gates on AUTH_REQUIRED and auth module."""

    @pytest.mark.asyncio
    async def test_auth_not_required_returns_true(self, monkeypatch):
        import bridge

        monkeypatch.setattr(bridge, "AUTH_REQUIRED", False)
        update = MagicMock()
        result = await bridge._check_auth(update)
        assert result is True

    @pytest.mark.asyncio
    async def test_auth_required_authenticated(self, monkeypatch):
        import auth
        import bridge

        monkeypatch.setattr(bridge, "AUTH_REQUIRED", True)
        monkeypatch.setattr(auth, "is_authenticated", lambda uid: True)
        touch_calls = []
        monkeypatch.setattr(auth, "touch_session", lambda uid: touch_calls.append(uid))

        update = MagicMock()
        update.effective_user.id = 42
        result = await bridge._check_auth(update)
        assert result is True
        assert touch_calls == [42]

    @pytest.mark.asyncio
    async def test_auth_required_not_authenticated(self, monkeypatch):
        import auth
        import bridge

        monkeypatch.setattr(bridge, "AUTH_REQUIRED", True)
        monkeypatch.setattr(auth, "is_authenticated", lambda uid: False)

        update = MagicMock()
        update.effective_user.id = 99
        result = await bridge._check_auth(update)
        assert result is False

    @pytest.mark.asyncio
    async def test_touch_session_called_on_success(self, monkeypatch):
        import auth
        import bridge

        monkeypatch.setattr(bridge, "AUTH_REQUIRED", True)
        monkeypatch.setattr(auth, "is_authenticated", lambda uid: True)
        touched = []
        monkeypatch.setattr(auth, "touch_session", lambda uid: touched.append(uid))

        update = MagicMock()
        update.effective_user.id = 7
        await bridge._check_auth(update)
        assert 7 in touched

    @pytest.mark.asyncio
    async def test_touch_session_not_called_on_failure(self, monkeypatch):
        import auth
        import bridge

        monkeypatch.setattr(bridge, "AUTH_REQUIRED", True)
        monkeypatch.setattr(auth, "is_authenticated", lambda uid: False)
        touched = []
        monkeypatch.setattr(auth, "touch_session", lambda uid: touched.append(uid))

        update = MagicMock()
        update.effective_user.id = 8
        await bridge._check_auth(update)
        assert touched == []


# ---------------------------------------------------------------------------
# _send_auth_link
# ---------------------------------------------------------------------------


class TestSendAuthLink:
    """_send_auth_link generates a token and sends a link, or rejects rate-limited users."""

    @pytest.mark.asyncio
    async def test_normal_flow_sends_link(self, monkeypatch):
        import auth
        import bridge

        monkeypatch.setattr(auth, "is_rate_limited", lambda uid: False)
        monkeypatch.setattr(auth, "generate_auth_token", lambda uid: "tok-abc")
        monkeypatch.setattr(bridge, "AUTH_BASE_URL", "https://auth.example.com")

        update = MagicMock()
        update.effective_user.id = 50
        update.message.reply_text = AsyncMock()

        await bridge._send_auth_link(update)
        update.message.reply_text.assert_called_once()
        sent_text = update.message.reply_text.call_args[0][0]
        assert "https://auth.example.com/login?token=tok-abc" in sent_text
        assert "15 minutes" in sent_text

    @pytest.mark.asyncio
    async def test_rate_limited_sends_rejection(self, monkeypatch):
        import auth
        import bridge

        monkeypatch.setattr(auth, "is_rate_limited", lambda uid: True)

        update = MagicMock()
        update.effective_user.id = 60
        update.message.reply_text = AsyncMock()

        await bridge._send_auth_link(update)
        update.message.reply_text.assert_called_once()
        sent_text = update.message.reply_text.call_args[0][0]
        assert "too many failed attempts" in sent_text.lower()
        assert "15 minutes" in sent_text.lower()


# ---------------------------------------------------------------------------
# _notify_delivery_failure
# ---------------------------------------------------------------------------


class TestNotifyDeliveryFailure:
    """_notify_delivery_failure sends a best-effort error message."""

    @pytest.mark.asyncio
    async def test_sends_failure_message(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._notify_delivery_failure(bot, 111, None, "test-label")
        bot.send_message.assert_called_once()
        sent_text = bot.send_message.call_args.kwargs["text"]
        assert "could not be delivered" in sent_text

    @pytest.mark.asyncio
    async def test_with_thread_id(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._notify_delivery_failure(bot, 111, 77, "test-label")
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs["message_thread_id"] == 77
        assert kwargs["chat_id"] == 111

    @pytest.mark.asyncio
    async def test_without_thread_id(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._notify_delivery_failure(bot, 111, None, "test-label")
        kwargs = bot.send_message.call_args.kwargs
        assert "message_thread_id" not in kwargs

    @pytest.mark.asyncio
    async def test_suppresses_exception(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=Exception("notification also failed"))
        # Should NOT raise
        await bridge._notify_delivery_failure(bot, 111, None, "test-label")
        bot.send_message.assert_called_once()
