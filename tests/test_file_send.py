"""Tests for outbound file attachments via [[send-file: …]] sentinels."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from patchbay.file_send import (
    DOCUMENT_MAX_BYTES,
    PHOTO_MAX_BYTES,
    FileRequest,
    extract_file_sentinels,
    send_files,
)


# ---------------------------------------------------------------------------
# extract_file_sentinels
# ---------------------------------------------------------------------------


class TestExtract:
    def test_no_sentinels_returns_text_unchanged(self):
        text = "hello world"
        cleaned, reqs = extract_file_sentinels(text)
        assert cleaned == "hello world"
        assert reqs == []

    def test_single_sentinel_strips_and_extracts(self):
        text = "Here you go:\n[[send-file: /tmp/x.gpx]]\nThanks."
        cleaned, reqs = extract_file_sentinels(text)
        assert "send-file" not in cleaned
        assert "Here you go:" in cleaned
        assert "Thanks." in cleaned
        assert len(reqs) == 1
        assert reqs[0] == FileRequest(path=Path("/tmp/x.gpx"), caption=None)

    def test_caption_after_pipe(self):
        text = "[[send-file: /tmp/foo.png | look at this]]"
        cleaned, reqs = extract_file_sentinels(text)
        assert cleaned == ""
        assert reqs[0].caption == "look at this"

    def test_multiple_sentinels_preserve_order(self):
        text = "[[send-file: /a.md]]\n[[send-file: /b.png | b]]"
        _, reqs = extract_file_sentinels(text)
        assert [r.path for r in reqs] == [Path("/a.md"), Path("/b.png")]
        assert reqs[1].caption == "b"

    def test_inline_sentinel_strips_cleanly(self):
        text = "see [[send-file: /tmp/x.md]] above"
        cleaned, reqs = extract_file_sentinels(text)
        assert cleaned == "see  above"
        assert reqs[0].path == Path("/tmp/x.md")

    def test_collapses_blank_runs_after_strip(self):
        text = "line1\n\n[[send-file: /a]]\n\nline2"
        cleaned, _ = extract_file_sentinels(text)
        # Triple-newlines that the bare sentinel left behind should fold
        # back to a single blank-line gap.
        assert "\n\n\n" not in cleaned

    def test_sentinel_inside_inline_code_is_ignored(self):
        text = "Use `[[send-file: /abs/path.png]]` in your response."
        cleaned, requests = extract_file_sentinels(text)
        assert requests == []
        assert "`[[send-file: /abs/path.png]]`" in cleaned

    def test_sentinel_inside_fenced_block_is_ignored(self):
        text = "Example:\n```\n[[send-file: /abs/path.png]]\n```\nDone."
        cleaned, requests = extract_file_sentinels(text)
        assert requests == []
        assert "[[send-file: /abs/path.png]]" in cleaned

    def test_real_sentinel_alongside_code_example(self):
        text = "Use `[[send-file: /example]]` like this:\n[[send-file: /real/file.png]]"
        cleaned, requests = extract_file_sentinels(text)
        assert len(requests) == 1
        assert str(requests[0].path) == "/real/file.png"
        assert "`[[send-file: /example]]`" in cleaned


# ---------------------------------------------------------------------------
# send_files
# ---------------------------------------------------------------------------


@pytest.fixture
def bot():
    b = MagicMock()
    b.send_photo = AsyncMock()
    b.send_document = AsyncMock()
    b.send_message = AsyncMock()
    return b


@pytest.mark.asyncio
async def test_image_uses_send_photo(bot, tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    await send_files(
        bot,
        chat_id=111,
        thread_id=22,
        session_key="111_22",
        requests=[FileRequest(path=p, caption=None)],
    )
    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_not_awaited()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == 111
    assert kwargs["message_thread_id"] == 22


@pytest.mark.asyncio
async def test_non_image_uses_send_document(bot, tmp_path):
    p = tmp_path / "data.gpx"
    p.write_text("<gpx/>")
    await send_files(
        bot,
        chat_id=111,
        thread_id=None,
        session_key="111_0",
        requests=[FileRequest(path=p, caption="trail")],
    )
    bot.send_document.assert_awaited_once()
    bot.send_photo.assert_not_awaited()
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["caption"] == "trail"
    assert "message_thread_id" not in kwargs


@pytest.mark.asyncio
async def test_missing_file_reports_failure(bot, tmp_path):
    missing = tmp_path / "nope.md"
    await send_files(
        bot,
        chat_id=1,
        thread_id=None,
        session_key="k",
        requests=[FileRequest(path=missing, caption=None)],
    )
    bot.send_document.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    note = bot.send_message.await_args.kwargs["text"]
    assert "missing" in note


@pytest.mark.asyncio
async def test_relative_path_rejected(bot, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.md").write_text("hi")
    await send_files(
        bot,
        chat_id=1,
        thread_id=None,
        session_key="k",
        requests=[FileRequest(path=Path("x.md"), caption=None)],
    )
    bot.send_document.assert_not_awaited()
    note = bot.send_message.await_args.kwargs["text"]
    assert "path_not_absolute" in note


@pytest.mark.asyncio
async def test_oversize_photo_rejected(bot, tmp_path, monkeypatch):
    p = tmp_path / "big.png"
    p.write_bytes(b"\x00")

    # Override _classify to report a size larger than the photo cap.
    # Patching Path.stat directly breaks is_file(), which also calls stat.
    from patchbay import file_send as fs

    monkeypatch.setattr(fs, "_classify", lambda _path: ("photo", PHOTO_MAX_BYTES + 1))
    await send_files(
        bot,
        chat_id=1,
        thread_id=None,
        session_key="k",
        requests=[FileRequest(path=p, caption=None)],
    )
    bot.send_photo.assert_not_awaited()
    note = bot.send_message.await_args.kwargs["text"]
    assert "too_large" in note


@pytest.mark.asyncio
async def test_send_exception_is_swallowed(bot, tmp_path):
    p = tmp_path / "x.md"
    p.write_text("hi")
    bot.send_document.side_effect = RuntimeError("boom")
    # Must not raise.
    await send_files(
        bot,
        chat_id=1,
        thread_id=None,
        session_key="k",
        requests=[FileRequest(path=p, caption=None)],
    )
    bot.send_message.assert_awaited()
    note = bot.send_message.await_args.kwargs["text"]
    assert "send_failed" in note
    assert "RuntimeError" in note


@pytest.mark.asyncio
async def test_multiple_requests_sent_in_order(bot, tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("a")
    b.write_text("b")
    await send_files(
        bot,
        chat_id=1,
        thread_id=None,
        session_key="k",
        requests=[
            FileRequest(path=a, caption=None),
            FileRequest(path=b, caption=None),
        ],
    )
    assert bot.send_document.await_count == 2


# ---------------------------------------------------------------------------
# Cap constants are sane
# ---------------------------------------------------------------------------


def test_caps_are_telegram_limits():
    assert PHOTO_MAX_BYTES == 10 * 1024 * 1024
    assert DOCUMENT_MAX_BYTES == 50 * 1024 * 1024
