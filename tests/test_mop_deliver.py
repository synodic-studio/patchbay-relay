"""Telegram delivery closure for MOP.

The deliver closure schedules sends on the bridge's main asyncio loop
via `run_coroutine_threadsafe`. In-process tests run with a single loop
(pytest-asyncio creates one), so passing `asyncio.get_event_loop()`
makes the cross-loop call resolve immediately on the same loop.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from patchbay.mop_deliver import build_telegram_deliver


@pytest.mark.asyncio
async def test_deliver_text_calls_send_message():
    bot = AsyncMock()
    deliver = build_telegram_deliver(
        bot=bot, chat_id=42, thread_id=None, main_loop=asyncio.get_event_loop()
    )
    await deliver("hello", None)
    bot.send_message.assert_called_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["text"] == "hello"


@pytest.mark.asyncio
async def test_deliver_with_thread_id_passes_message_thread_id():
    bot = AsyncMock()
    deliver = build_telegram_deliver(
        bot=bot, chat_id=42, thread_id=7, main_loop=asyncio.get_event_loop()
    )
    await deliver("hello", None)
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["message_thread_id"] == 7


@pytest.mark.asyncio
async def test_deliver_system_note_sends_second_message_with_marker():
    bot = AsyncMock()
    deliver = build_telegram_deliver(
        bot=bot, chat_id=42, thread_id=None, main_loop=asyncio.get_event_loop()
    )
    await deliver("user message", "MOP failed-open after 4 attempts")
    assert bot.send_message.call_count == 2
    first_kwargs = bot.send_message.call_args_list[0].kwargs
    second_kwargs = bot.send_message.call_args_list[1].kwargs
    assert first_kwargs["text"] == "user message"
    # Second message is the system note, formatted distinctly.
    assert "failed-open" in second_kwargs["text"]
    assert "⚠️" in second_kwargs["text"] or "MOP" in second_kwargs["text"]


# ---------------------------------------------------------------------------
# File-attachment sentinels — `[[send-file: /abs/path | caption]]`
# ---------------------------------------------------------------------------


def _file_bot():
    """A bot mock with the methods send_files needs."""
    b = MagicMock()
    b.send_message = AsyncMock()
    b.send_photo = AsyncMock()
    b.send_document = AsyncMock()
    return b


@pytest.mark.asyncio
async def test_deliver_extracts_file_sentinel_and_strips_text(tmp_path):
    """Sentinels are pulled out, the file ships as an attachment, and the
    user-visible text no longer contains the sentinel."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    bot = _file_bot()
    deliver = build_telegram_deliver(
        bot=bot,
        chat_id=42,
        thread_id=7,
        main_loop=asyncio.get_event_loop(),
        session_key="42_7",
    )
    await deliver(
        f"Here you go.\n\n[[send-file: {pdf} | the doc]]",
        None,
    )
    # Text was sent (without the sentinel), document was sent.
    bot.send_message.assert_awaited_once()
    assert "send-file" not in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["text"].strip() == "Here you go."
    bot.send_document.assert_awaited_once()
    doc_kwargs = bot.send_document.await_args.kwargs
    assert doc_kwargs["chat_id"] == 42
    assert doc_kwargs["message_thread_id"] == 7
    assert doc_kwargs["caption"] == "the doc"


@pytest.mark.asyncio
async def test_deliver_sentinel_only_skips_text_send(tmp_path):
    """If the message is only a sentinel, no empty text message is sent."""
    pdf = tmp_path / "only.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    bot = _file_bot()
    deliver = build_telegram_deliver(
        bot=bot,
        chat_id=1,
        thread_id=None,
        main_loop=asyncio.get_event_loop(),
        session_key="1_0",
    )
    await deliver(f"[[send-file: {pdf}]]", None)
    bot.send_message.assert_not_awaited()
    bot.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_deliver_image_sentinel_uses_send_photo(tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    bot = _file_bot()
    deliver = build_telegram_deliver(
        bot=bot,
        chat_id=1,
        thread_id=None,
        main_loop=asyncio.get_event_loop(),
        session_key="1_0",
    )
    await deliver(f"see [[send-file: {img}]]", None)
    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_sentinel_inside_code_fence_is_not_extracted(tmp_path):
    """A sentinel inside backticks is documentation, not a real request."""
    bot = _file_bot()
    deliver = build_telegram_deliver(
        bot=bot,
        chat_id=1,
        thread_id=None,
        main_loop=asyncio.get_event_loop(),
        session_key="1_0",
    )
    await deliver("Example: `[[send-file: /tmp/x.pdf]]`", None)
    bot.send_message.assert_awaited_once()
    assert "send-file" in bot.send_message.await_args.kwargs["text"]
    bot.send_document.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
