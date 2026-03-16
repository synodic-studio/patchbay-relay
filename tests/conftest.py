"""Shared pytest fixtures for stargate tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def tmp_auth_state(tmp_path):
    """Isolate auth state to a temporary directory.

    Patches auth.AUTH_STATE_FILE, auth.AUTH_LOG_FILE, and auth.AUTH_DIR so
    tests never read from or write to the real auth/ directory.  The temp
    paths are returned as a dict for tests that want to inspect them directly.
    """
    tmp_auth_dir = tmp_path / "auth"
    tmp_auth_dir.mkdir()
    tmp_state_file = tmp_auth_dir / "sessions.json"
    tmp_log_file = tmp_auth_dir / "auth_log.jsonl"

    import auth

    tmp_lock_file = tmp_auth_dir / ".sessions.lock"

    with (
        patch.object(auth, "AUTH_DIR", tmp_auth_dir),
        patch.object(auth, "AUTH_STATE_FILE", tmp_state_file),
        patch.object(auth, "AUTH_LOG_FILE", tmp_log_file),
        patch.object(auth, "_AUTH_LOCK_FILE", tmp_lock_file),
    ):
        # Reset in-memory rate-limit state between tests
        auth._failed_attempts.clear()
        yield {
            "dir": tmp_auth_dir,
            "state_file": tmp_state_file,
            "log_file": tmp_log_file,
            "lock_file": tmp_lock_file,
        }


@pytest.fixture
def mock_bot():
    """Return a mock Telegram Application with a pre-wired bot.

    Provides async stubs for the most-used bot methods so tests that exercise
    bridge handlers don't need to set up real network connections.

    Usage::

        async def test_something(mock_bot):
            await bridge.some_handler(update, mock_bot["context"])
            mock_bot["bot"].send_message.assert_called_once()
    """
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_chat_action = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_document = AsyncMock()
    bot.answer_callback_query = AsyncMock()

    application = MagicMock()
    application.bot = bot

    context = MagicMock()
    context.bot = bot
    context.application = application

    return {
        "bot": bot,
        "application": application,
        "context": context,
    }
