"""Telegram delivery closure for MOP v2."""

from unittest.mock import AsyncMock

import pytest

from patchbay.mop_deliver import build_telegram_deliver


@pytest.mark.asyncio
async def test_deliver_text_calls_send_message():
    bot = AsyncMock()
    deliver = build_telegram_deliver(bot=bot, chat_id=42, thread_id=None)
    await deliver("hello", None)
    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["text"] == "hello"


@pytest.mark.asyncio
async def test_deliver_with_thread_id_passes_message_thread_id():
    bot = AsyncMock()
    deliver = build_telegram_deliver(bot=bot, chat_id=42, thread_id=7)
    await deliver("hello", None)
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["message_thread_id"] == 7


@pytest.mark.asyncio
async def test_deliver_system_note_sends_second_message_with_marker():
    bot = AsyncMock()
    deliver = build_telegram_deliver(bot=bot, chat_id=42, thread_id=None)
    await deliver("user message", "MOP failed-open after 4 attempts")
    assert bot.send_message.call_count == 2
    first_kwargs = bot.send_message.call_args_list[0].kwargs
    second_kwargs = bot.send_message.call_args_list[1].kwargs
    assert first_kwargs["text"] == "user message"
    # Second message is the system note, formatted distinctly.
    assert "failed-open" in second_kwargs["text"]
    assert "⚠️" in second_kwargs["text"] or "MOP" in second_kwargs["text"]
