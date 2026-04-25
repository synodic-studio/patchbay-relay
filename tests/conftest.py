"""Shared pytest fixtures for stargate tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest


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
