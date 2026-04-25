"""Tests for P1 robustness fixes: retry logic, file locking, graceful shutdown,
and session key sanitization."""

import signal
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fix 1: Telegram send retry with exponential backoff (CTB-5cs)
# ---------------------------------------------------------------------------


class TestSendRetry:
    """_send_response retries transient Telegram API failures."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import bridge
        import stargate.sessions

        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        # Speed up retries for tests
        monkeypatch.setattr(bridge, "SEND_RETRY_BASE_DELAY", 0.01)

    @pytest.mark.asyncio
    async def test_succeeds_on_first_try(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._send_response(bot, 123, None, "hello")
        bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_on_transient_failure(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[Exception("network error"), None])
        await bridge._send_response(bot, 123, None, "hello")
        assert bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=Exception("persistent failure"))
        with pytest.raises(Exception, match="persistent failure"):
            await bridge._send_response(bot, 123, None, "hello")
        assert bot.send_message.call_count == bridge.SEND_RETRY_ATTEMPTS

    @pytest.mark.asyncio
    async def test_retry_with_thread_id(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[Exception("timeout"), None])
        await bridge._send_response(bot, 123, 456, "hello")
        # Verify thread_id was passed in both attempts
        for call in bot.send_message.call_args_list:
            assert call.kwargs.get("message_thread_id") == 456

    @pytest.mark.asyncio
    async def test_succeeds_on_third_attempt(self):
        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[Exception("err1"), Exception("err2"), None])
        await bridge._send_response(bot, 123, None, "hello")
        assert bot.send_message.call_count == 3


# ---------------------------------------------------------------------------
# Fix 3: Graceful shutdown (CTB-62n)
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    """Graceful shutdown stops accepting new messages and cleans up."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import bridge
        import stargate.sessions

        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        monkeypatch.setattr(bridge, "PHOTO_DIR", tmp_path / "photos")
        (tmp_path / "photos").mkdir()
        # Reset shutdown flag
        bridge._shutting_down = False

    def test_shutting_down_flag_starts_false(self):
        import bridge

        assert bridge._shutting_down is False

    @pytest.mark.asyncio
    async def test_handle_message_rejects_during_shutdown(self):
        import bridge

        bridge._shutting_down = True
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        await bridge.handle_message(update, context)
        update.message.reply_text.assert_called_once()
        msg = update.message.reply_text.call_args[0][0]
        assert "shutting down" in msg.lower()

    @pytest.mark.asyncio
    async def test_handle_photo_rejects_during_shutdown(self):
        import bridge

        bridge._shutting_down = True
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        await bridge.handle_photo(update, context)
        update.message.reply_text.assert_called_once()
        msg = update.message.reply_text.call_args[0][0]
        assert "shutting down" in msg.lower()

    def test_graceful_shutdown_sets_flag(self, monkeypatch):
        import bridge

        # Prevent sys.exit from actually exiting
        monkeypatch.setattr(bridge, "_sessions", {})
        monkeypatch.setattr(bridge, "_remote_proc", None)
        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(signal.SIGTERM, None)
        assert bridge._shutting_down is True

    def test_graceful_shutdown_terminates_active_procs(self, monkeypatch):
        import bridge

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.wait.return_value = None
        monkeypatch.setattr(bridge, "_sessions", {"test_key": bridge.SessionState(proc=mock_proc)})
        monkeypatch.setattr(bridge, "_remote_proc", None)
        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(signal.SIGTERM, None)
        mock_proc.terminate.assert_called_once()

    def test_graceful_shutdown_cleans_temp_photos(self, monkeypatch, tmp_path):
        import bridge

        photo_dir = tmp_path / "shutdown_photos"
        photo_dir.mkdir()
        (photo_dir / "test1.jpg").write_text("fake")
        (photo_dir / "test2.jpg").write_text("fake")
        monkeypatch.setattr(bridge, "PHOTO_DIR", photo_dir)
        monkeypatch.setattr(bridge, "_sessions", {})
        monkeypatch.setattr(bridge, "_remote_proc", None)
        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(signal.SIGTERM, None)
        assert not list(photo_dir.glob("*.jpg"))


# ---------------------------------------------------------------------------
# Fix 4: Path traversal prevention in session keys (CTB-api)
# ---------------------------------------------------------------------------


class TestSessionKeySanitization:
    """_sanitize_session_key blocks path traversal attempts."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import stargate.sessions

        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()

    def test_normal_key_passes(self):
        import bridge

        assert bridge._sanitize_session_key("12345_67") == "12345_67"

    def test_numeric_key_passes(self):
        import bridge

        assert bridge._sanitize_session_key("12345") == "12345"

    def test_key_with_hyphen_passes(self):
        import bridge

        assert bridge._sanitize_session_key("REDACTED_GROUP_1_327") == "REDACTED_GROUP_1_327"

    def test_path_traversal_stripped(self):
        import bridge

        # "../../../etc/passwd" should have path components stripped
        with pytest.raises(ValueError, match="Invalid session key"):
            bridge._sanitize_session_key("../../../etc/passwd")

    def test_absolute_path_stripped(self):
        import bridge

        with pytest.raises(ValueError, match="Invalid session key"):
            bridge._sanitize_session_key("/etc/passwd")

    def test_dot_slash_stripped(self):
        import bridge

        with pytest.raises(ValueError, match="Invalid session key"):
            bridge._sanitize_session_key("./sessions/12345")

    def test_empty_key_rejected(self):
        import bridge

        with pytest.raises(ValueError, match="Invalid session key"):
            bridge._sanitize_session_key("")

    def test_special_chars_rejected(self):
        import bridge

        with pytest.raises(ValueError, match="Invalid session key"):
            bridge._sanitize_session_key("key;rm -rf /")

    def test_get_session_id_sanitizes(self):
        """get_session_id applies sanitization — path traversal raises."""
        import bridge

        with pytest.raises(ValueError):
            bridge.get_session_id("../../etc/passwd")

    def test_save_session_id_sanitizes(self):
        import bridge

        with pytest.raises(ValueError):
            bridge.save_session_id("../../etc/passwd", "sess-123")

    def test_clear_session_sanitizes(self):
        import bridge

        with pytest.raises(ValueError):
            bridge.clear_session("../../etc/passwd")

    def test_normal_session_roundtrip_works(self):
        """Normal session keys still work after sanitization is added."""
        import bridge

        bridge.save_session_id("-123_456", "sess-abc")
        assert bridge.get_session_id("-123_456") == "sess-abc"
        bridge.clear_session("-123_456")
        assert bridge.get_session_id("-123_456") is None
