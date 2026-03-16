"""Tests for bridge.handle_photo — photo download, Claude invocation,
response delivery, and error paths."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge
import stargate.sessions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_photo_update(
    user_id=42, chat_id=1, thread_id=None, caption=None, file_id="fid", unique_id="uid"
):
    """Build a minimal mock Update with a photo attachment."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.message_thread_id = thread_id
    update.message.caption = caption
    update.message.reply_text = AsyncMock()

    photo_obj = MagicMock()
    photo_obj.file_id = file_id
    photo_obj.file_unique_id = unique_id
    update.message.photo = [MagicMock(), photo_obj]  # low-res, high-res

    return update


def _make_context(bot=None):
    ctx = MagicMock()
    if bot is None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_chat_action = AsyncMock()
    ctx.bot = bot

    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock()
    bot.get_file = AsyncMock(return_value=tg_file)

    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHandlePhoto:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        monkeypatch.setattr(bridge, "PHOTO_DIR", tmp_path / "photos")
        (tmp_path / "photos").mkdir()
        monkeypatch.setattr(bridge, "PENDING_DIR", tmp_path / "pending")
        (tmp_path / "pending").mkdir()
        monkeypatch.setattr(stargate.sessions, "PENDING_DIR", tmp_path / "pending")
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", False)
        monkeypatch.setattr(bridge, "SEND_RETRY_BASE_DELAY", 0.01)
        bridge._shutting_down = False
        bridge._processing_sessions.clear()
        self._tmp = tmp_path

    @pytest.mark.asyncio
    async def test_rejects_during_shutdown(self):
        bridge._shutting_down = True
        update = _make_photo_update()
        ctx = _make_context()
        await bridge.handle_photo(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "shutting down" in msg.lower()

    @pytest.mark.asyncio
    async def test_rejects_unauthorized_user(self, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", {9999})
        update = _make_photo_update(user_id=42)
        ctx = _make_context()
        await bridge.handle_photo(update, ctx)
        # Should silently return — no reply
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_auth_link_when_not_authenticated(self, monkeypatch, tmp_auth_state):
        monkeypatch.setattr(bridge, "AUTH_REQUIRED", True)
        update = _make_photo_update(user_id=42)
        ctx = _make_context()
        await bridge.handle_photo(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "Authentication required" in reply or "auth" in reply.lower()

    @pytest.mark.asyncio
    async def test_successful_photo_processing(self):
        update = _make_photo_update(caption="What is this?")
        ctx = _make_context()

        with patch.object(bridge, "run_claude", return_value="It is a cat."):
            await bridge.handle_photo(update, ctx)

        # Response should have been sent
        ctx.bot.send_message.assert_called()
        sent_text = ctx.bot.send_message.call_args.kwargs.get(
            "text", ctx.bot.send_message.call_args[1].get("text", "")
        )
        assert "cat" in sent_text

    @pytest.mark.asyncio
    async def test_default_caption_when_none(self):
        update = _make_photo_update(caption=None)
        ctx = _make_context()

        with patch.object(bridge, "run_claude", return_value="Description here.") as mock_claude:
            await bridge.handle_photo(update, ctx)
            prompt_arg = mock_claude.call_args[0][0]
            assert "Describe this image" in prompt_arg

    @pytest.mark.asyncio
    async def test_claude_error_is_caught(self):
        update = _make_photo_update()
        ctx = _make_context()

        with patch.object(bridge, "run_claude", side_effect=RuntimeError("boom")):
            await bridge.handle_photo(update, ctx)

        ctx.bot.send_message.assert_called()
        sent_text = ctx.bot.send_message.call_args.kwargs.get(
            "text", ctx.bot.send_message.call_args[1].get("text", "")
        )
        assert "Error" in sent_text

    @pytest.mark.asyncio
    async def test_delivery_failure_notifies(self):
        update = _make_photo_update()
        ctx = _make_context()

        with patch.object(bridge, "run_claude", return_value="good response"):
            # Make _send_response fail, then succeed on the notification
            ctx.bot.send_message = AsyncMock(
                side_effect=[Exception("telegram down")] * 3 + [None]
            )
            await bridge.handle_photo(update, ctx)

        # The notification about delivery failure should have been attempted
        assert ctx.bot.send_message.call_count >= 3

    @pytest.mark.asyncio
    async def test_photo_file_cleaned_up(self):
        update = _make_photo_update(unique_id="cleanup-test")
        ctx = _make_context()

        photo_path = self._tmp / "photos" / "cleanup-test.jpg"
        photo_path.write_text("fake image")

        with patch.object(bridge, "run_claude", return_value="done"):
            await bridge.handle_photo(update, ctx)

        # File should be cleaned up after processing
        assert not photo_path.exists()

    @pytest.mark.asyncio
    async def test_downloads_highest_resolution(self):
        update = _make_photo_update(file_id="hi-res-id")
        ctx = _make_context()

        with patch.object(bridge, "run_claude", return_value="ok"):
            await bridge.handle_photo(update, ctx)

        # Should have called get_file with the highest-res photo's file_id
        ctx.bot.get_file.assert_called_once_with("hi-res-id")

    @pytest.mark.asyncio
    async def test_thread_id_passed_in_response(self):
        update = _make_photo_update(chat_id=100, thread_id=200)
        ctx = _make_context()

        with patch.object(bridge, "run_claude", return_value="response"):
            await bridge.handle_photo(update, ctx)

        call_kwargs = ctx.bot.send_message.call_args.kwargs
        assert call_kwargs.get("message_thread_id") == 200
        assert call_kwargs.get("chat_id") == 100
