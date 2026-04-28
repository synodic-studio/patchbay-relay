"""Tests for Telegram helper functions in bridge.py:
_send_response, keep_typing, _notify_delivery_failure.
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
        # Disable MarkdownV2 conversion so chunking tests can assert
        # verbatim chunk contents. Dedicated markdown tests below cover
        # the MarkdownV2 send path.
        monkeypatch.setattr(bridge, "_to_markdownv2", lambda _text: None)

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
        text = "D" * (4096 * 4 + 1)
        await bridge._send_response(bot, 111, None, text)
        assert bot.send_message.call_count == 5
        reassembled = "".join(call.kwargs["text"] for call in bot.send_message.call_args_list)
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
# MarkdownV2 rendering
# ---------------------------------------------------------------------------


class TestMarkdownV2:
    """_send_response renders MarkdownV2 when conversion succeeds."""

    @pytest.fixture(autouse=True)
    def _fast_retries(self, monkeypatch):
        import bridge

        monkeypatch.setattr(bridge, "SEND_RETRY_BASE_DELAY", 0.0)
        monkeypatch.setattr(bridge, "SEND_RETRY_ATTEMPTS", 3)

    @pytest.mark.asyncio
    async def test_sends_with_markdownv2_parse_mode(self):
        import bridge
        from telegram.constants import ParseMode

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._send_response(bot, 111, None, "**bold** and _italic_")
        bot.send_message.assert_called_once()
        assert bot.send_message.call_args.kwargs["parse_mode"] == ParseMode.MARKDOWN_V2

    @pytest.mark.asyncio
    async def test_falls_back_to_plain_on_markdown_send_error(self):
        import bridge

        bot = MagicMock()
        # First call (MarkdownV2) raises; subsequent plain call succeeds.
        bot.send_message = AsyncMock(side_effect=[Exception("bad entity"), None])
        await bridge._send_response(bot, 111, None, "whatever")
        assert bot.send_message.call_count == 2
        assert "parse_mode" in bot.send_message.call_args_list[0].kwargs
        assert "parse_mode" not in bot.send_message.call_args_list[1].kwargs

    @pytest.mark.asyncio
    async def test_falls_back_to_plain_when_conversion_returns_none(self, monkeypatch):
        import bridge

        monkeypatch.setattr(bridge, "_to_markdownv2", lambda _text: None)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._send_response(bot, 111, None, "plain text")
        bot.send_message.assert_called_once()
        assert "parse_mode" not in bot.send_message.call_args.kwargs

    @pytest.mark.asyncio
    async def test_one_failed_chunk_conversion_drops_whole_response_to_plain(
        self, monkeypatch
    ):
        """Audit §13: a multi-chunk response must not ship some chunks as
        MarkdownV2 and others as plain. If any chunk fails to convert, every
        chunk goes plain."""
        import bridge

        monkeypatch.setattr(bridge, "TELEGRAM_MSG_LIMIT", 10)

        # 3-chunk response. Conversion of the middle chunk returns None;
        # the other two would convert fine. Whole response should still go plain.
        responses = ["MD0", None, "MD2"]

        def fake_md(text):
            return responses.pop(0) if responses else "MD?"

        monkeypatch.setattr(bridge, "_to_markdownv2", fake_md)

        bot = MagicMock()
        bot.send_message = AsyncMock()

        await bridge._send_response(bot, 111, None, "AAAAAAAAAA" + "BBBBBBBBBB" + "CCCCCCCCCC")
        # 3 chunks, all plain
        assert bot.send_message.call_count == 3
        for call in bot.send_message.call_args_list:
            assert "parse_mode" not in call.kwargs

    @pytest.mark.asyncio
    async def test_conversion_failure_logged_to_activity_with_raw_text(
        self, monkeypatch
    ):
        """When telegramify_markdown raises, the raw text and exception are
        captured to activity.jsonl so we can reproduce converter regressions."""
        import bridge

        events: list[dict] = []
        monkeypatch.setattr(bridge, "_log_activity", lambda evt, **kw: events.append({"event": evt, **kw}))

        def _explode(_text):
            raise ValueError("malformed markdown")

        monkeypatch.setattr(bridge, "telegramify_markdown", type("M", (), {"markdownify": staticmethod(_explode)}))

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._send_response(bot, 111, None, "boom **inside**")

        # Sent as plain
        assert "parse_mode" not in bot.send_message.call_args.kwargs
        # Activity log has the failure recorded
        failures = [e for e in events if e["event"] == "markdown_conversion_failed"]
        assert failures, "expected a markdown_conversion_failed event"
        f = failures[0]
        assert f["error_type"] == "ValueError"
        assert "malformed markdown" in f["error"]
        assert f["raw_text"] == "boom **inside**"
        assert f["raw_text_len"] == len("boom **inside**")

    @pytest.mark.asyncio
    async def test_send_failure_logs_raw_and_md_to_activity(self, monkeypatch):
        """When a MarkdownV2 send is rejected by Telegram, the raw text and
        the converted MD payload are both captured so we can diagnose."""
        import bridge

        events: list[dict] = []
        monkeypatch.setattr(bridge, "_log_activity", lambda evt, **kw: events.append({"event": evt, **kw}))
        monkeypatch.setattr(bridge, "_to_markdownv2", lambda t: f"MD::{t}")

        bot = MagicMock()
        # MarkdownV2 send fails on first attempt, plain succeeds on retry.
        bot.send_message = AsyncMock(side_effect=[Exception("Bad entity"), None])

        await bridge._send_response(bot, 111, None, "raw payload")

        failures = [e for e in events if e["event"] == "markdown_send_failed"]
        assert failures, "expected a markdown_send_failed event"
        f = failures[0]
        assert f["error_type"] == "Exception"
        assert "Bad entity" in f["error"]
        assert f["raw_text"] == "raw payload"
        assert f["md_text"] == "MD::raw payload"

    @pytest.mark.asyncio
    async def test_failure_log_truncates_huge_text(self, monkeypatch):
        """Activity log should not blow up on multi-megabyte responses;
        raw text is capped to a reasonable preview length."""
        import bridge

        events: list[dict] = []
        monkeypatch.setattr(bridge, "_log_activity", lambda evt, **kw: events.append({"event": evt, **kw}))

        def _explode(_text):
            raise ValueError("nope")

        monkeypatch.setattr(bridge, "telegramify_markdown", type("M", (), {"markdownify": staticmethod(_explode)}))

        bot = MagicMock()
        bot.send_message = AsyncMock()
        # Use a single chunk that's larger than the failure-log preview cap
        # but still under TELEGRAM_MSG_LIMIT so the per-chunk len matches.
        huge_chunk = "x" * (bridge._MARKDOWN_FAILURE_TEXT_LIMIT + 200)
        await bridge._send_response(bot, 111, None, huge_chunk)

        failures = [e for e in events if e["event"] == "markdown_conversion_failed"]
        assert failures
        f = failures[0]
        assert f["truncated"] is True
        assert len(f["raw_text"]) <= bridge._MARKDOWN_FAILURE_TEXT_LIMIT
        assert f["raw_text_len"] == bridge._MARKDOWN_FAILURE_TEXT_LIMIT + 200

    @pytest.mark.asyncio
    async def test_midresponse_md_send_failure_downgrades_remaining_chunks(
        self, monkeypatch
    ):
        """If chunk N's MarkdownV2 send fails, chunks N+1, N+2, … are sent
        plain even if their conversion succeeded — preventing a half-formatted
        message past the failure point."""
        import bridge

        monkeypatch.setattr(bridge, "TELEGRAM_MSG_LIMIT", 10)
        monkeypatch.setattr(bridge, "_to_markdownv2", lambda t: f"MD::{t}")

        bot = MagicMock()
        # Chunk 0 succeeds as MarkdownV2 (one call). Chunk 1's MarkdownV2
        # send fails, then plain succeeds (two calls). Chunk 2 should go
        # straight to plain (one call) — no markdown attempt.
        side_effects = [
            None,                # chunk 0 markdown OK
            Exception("bad"),    # chunk 1 markdown fails
            None,                # chunk 1 plain retry OK
            None,                # chunk 2 plain (no markdown attempt)
        ]
        bot.send_message = AsyncMock(side_effect=side_effects)

        await bridge._send_response(bot, 111, None, "AAAAAAAAAA" + "BBBBBBBBBB" + "CCCCCCCCCC")
        assert bot.send_message.call_count == 4
        calls = bot.send_message.call_args_list
        # chunk 0: markdown
        assert "parse_mode" in calls[0].kwargs
        # chunk 1 first attempt: markdown
        assert "parse_mode" in calls[1].kwargs
        # chunk 1 retry: plain
        assert "parse_mode" not in calls[2].kwargs
        # chunk 2: plain (downgrade propagated)
        assert "parse_mode" not in calls[3].kwargs


# ---------------------------------------------------------------------------
# Outbound audit logging
# ---------------------------------------------------------------------------


class TestOutboundAudit:
    """_send_response writes an audit entry per chunk to outbound/."""

    @pytest.fixture(autouse=True)
    def _fast_retries(self, monkeypatch):
        import bridge

        monkeypatch.setattr(bridge, "SEND_RETRY_BASE_DELAY", 0.0)
        monkeypatch.setattr(bridge, "SEND_RETRY_ATTEMPTS", 3)

    @pytest.fixture
    def outbound_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("patchbay.outbound.OUTBOUND_DIR", tmp_path)
        return tmp_path

    @pytest.mark.asyncio
    async def test_logs_successful_markdownv2_send(self, outbound_dir):
        import json

        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bridge._send_response(bot, 111, None, "**bold**")
        lines = (outbound_dir / "111.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["source"] == "claude-response"
        assert entry["parse_mode"] == "MarkdownV2"
        assert entry["http_status"] == "ok"
        assert entry["chunk_index"] == 0
        assert entry["chunk_total"] == 1
        assert entry["raw_text"] == "**bold**"
        assert entry["md_text"] is not None

    @pytest.mark.asyncio
    async def test_logs_plain_fallback_after_markdownv2_failure(self, outbound_dir):
        """When MarkdownV2 raises and the plain retry succeeds, the audit
        entry reflects the parse_mode actually delivered to Telegram (plain)."""
        import json

        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[Exception("bad entity"), None])
        await bridge._send_response(bot, 111, None, "whatever")
        assert bot.send_message.call_count == 2
        lines = (outbound_dir / "111.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["parse_mode"] == "plain"
        assert entry["md_text"] is None
        assert entry["http_status"] == "ok"

    @pytest.mark.asyncio
    async def test_logs_failure_after_all_retries(self, outbound_dir):
        """When every retry fails, a single final audit entry captures
        the exception class name under http_status."""
        import json

        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
        with pytest.raises(RuntimeError):
            await bridge._send_response(bot, 111, None, "hello")
        lines = (outbound_dir / "111.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["source"] == "claude-response"
        assert entry["http_status"] == "RuntimeError"
        assert entry["chunk_index"] == 0
        assert entry["chunk_total"] == 1

    @pytest.mark.asyncio
    async def test_logs_one_entry_per_chunk_with_thread_id(self, outbound_dir):
        """A multi-chunk response produces one audit entry per chunk,
        with session_key including the thread_id."""
        import json

        import bridge

        bot = MagicMock()
        bot.send_message = AsyncMock()
        text = "X" * (bridge.TELEGRAM_MSG_LIMIT + 50)
        await bridge._send_response(bot, 111, 42, text)
        lines = (outbound_dir / "111_42.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        entries = [json.loads(line) for line in lines]
        assert [e["chunk_index"] for e in entries] == [0, 1]
        assert all(e["chunk_total"] == 2 for e in entries)
        assert all(e["session_key"] == "111_42" for e in entries)


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
    async def test_handles_transient_send_chat_action_exception(self, caplog):
        """A transient exception is logged at WARNING but the loop continues."""
        import logging

        import bridge

        bot = MagicMock()
        call_count = {"n": 0}

        async def flaky_send(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("network blip")
            # subsequent calls succeed

        bot.send_chat_action = AsyncMock(side_effect=flaky_send)
        stop = asyncio.Event()

        async def _set_after_brief():
            await asyncio.sleep(0.03)
            stop.set()

        asyncio.create_task(_set_after_brief())
        with caplog.at_level(logging.WARNING, logger="bridge"):
            await bridge.keep_typing(333, None, stop, bot)

        assert bot.send_chat_action.call_count >= 2  # loop continued past the failure
        assert any("keep_typing failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_gives_up_after_max_consecutive_failures(self, monkeypatch, caplog):
        """After TYPING_MAX_FAILURES consecutive errors, keep_typing returns
        rather than spamming forever — the typing indicator should not look
        alive when the Telegram API is persistently failing."""
        import logging

        import bridge

        monkeypatch.setattr(bridge, "TYPING_MAX_FAILURES", 3)
        bot = MagicMock()
        bot.send_chat_action = AsyncMock(side_effect=Exception("auth failed"))
        stop = asyncio.Event()  # never set: only the give-up path can end the loop

        with caplog.at_level(logging.ERROR, logger="bridge"):
            await bridge.keep_typing(444, None, stop, bot)

        assert bot.send_chat_action.call_count == 3
        assert any("giving up" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_gives_up_immediately_on_forbidden(self, caplog):
        """Forbidden (bot blocked / kicked) is persistent — no point retrying."""
        import logging

        from telegram.error import Forbidden

        import bridge

        bot = MagicMock()
        bot.send_chat_action = AsyncMock(side_effect=Forbidden("blocked by user"))
        stop = asyncio.Event()

        with caplog.at_level(logging.WARNING, logger="bridge"):
            await bridge.keep_typing(666, None, stop, bot)

        assert bot.send_chat_action.call_count == 1  # one try, then gone
        assert any("persistent" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_gives_up_immediately_on_chat_migrated(self):
        """ChatMigrated means the chat_id has changed; no point retrying here."""
        from telegram.error import ChatMigrated

        import bridge

        bot = MagicMock()
        bot.send_chat_action = AsyncMock(side_effect=ChatMigrated(new_chat_id=-1000))
        stop = asyncio.Event()
        await bridge.keep_typing(777, None, stop, bot)
        assert bot.send_chat_action.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_after_does_not_count_toward_cap(self, monkeypatch):
        """RetryAfter is a server-requested backoff, not a failure. The
        give-up counter should NOT advance, and the requested retry_after
        should replace the default interval."""
        from telegram.error import RetryAfter

        import bridge

        monkeypatch.setattr(bridge, "TYPING_MAX_FAILURES", 2)
        monkeypatch.setattr(bridge, "TYPING_INTERVAL", 10.0)  # default would be long
        bot = MagicMock()
        call_log = []

        async def retry_then_ok(**kwargs):
            call_log.append(True)
            if len(call_log) <= 3:
                raise RetryAfter(retry_after=0.01)  # very short so test is fast

        bot.send_chat_action = AsyncMock(side_effect=retry_then_ok)
        stop = asyncio.Event()

        async def _set_after_brief():
            await asyncio.sleep(0.15)
            stop.set()

        asyncio.create_task(_set_after_brief())
        await bridge.keep_typing(888, None, stop, bot)

        # Would have given up after 2 failures if RetryAfter counted;
        # instead we made more calls and eventually succeeded.
        assert len(call_log) > 2

    @pytest.mark.asyncio
    async def test_success_resets_failure_counter(self, monkeypatch):
        """A successful send resets the consecutive-failure count, so one
        transient blip every few iterations never trips the give-up cap."""
        import bridge

        monkeypatch.setattr(bridge, "TYPING_MAX_FAILURES", 3)
        bot = MagicMock()
        call_log = []

        async def alternating(**kwargs):
            call_log.append(True)
            # Fail on every second call; never consecutive enough to trip 3.
            if len(call_log) % 2 == 0:
                raise Exception("blip")

        bot.send_chat_action = AsyncMock(side_effect=alternating)
        stop = asyncio.Event()

        async def _set_after_brief():
            await asyncio.sleep(0.08)
            stop.set()

        asyncio.create_task(_set_after_brief())
        await bridge.keep_typing(555, None, stop, bot)

        # Must have made more calls than TYPING_MAX_FAILURES without giving up.
        assert len(call_log) > 3


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
        await bridge._notify_delivery_failure(bot, 111, None, "test-label")
        bot.send_message.assert_called_once()


# ---------------------------------------------------------------------------
# _send_response × file sentinels
# ---------------------------------------------------------------------------


class TestSendResponseFileSentinels:
    """Sentinels in response text route to send_files and are stripped."""

    @pytest.fixture(autouse=True)
    def _fast_retries(self, monkeypatch):
        import bridge

        monkeypatch.setattr(bridge, "SEND_RETRY_BASE_DELAY", 0.0)
        monkeypatch.setattr(bridge, "SEND_RETRY_ATTEMPTS", 3)
        monkeypatch.setattr(bridge, "_to_markdownv2", lambda _t: None)

    @pytest.mark.asyncio
    async def test_sentinel_routes_to_send_document(self, tmp_path):
        import bridge

        p = tmp_path / "x.gpx"
        p.write_text("<gpx/>")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()
        bot.send_photo = AsyncMock()

        text = f"Here you go.\n[[send-file: {p}]]"
        await bridge._send_response(bot, 111, 22, text)

        # Text part sent (sentinel stripped).
        msg_text = bot.send_message.await_args.kwargs["text"]
        assert "send-file" not in msg_text
        assert "Here you go." in msg_text
        # File sent as document.
        bot.send_document.assert_awaited_once()
        bot.send_photo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sentinel_only_response_sends_no_text(self, tmp_path):
        import bridge

        p = tmp_path / "x.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_document = AsyncMock()

        await bridge._send_response(bot, 111, None, f"[[send-file: {p}]]")

        bot.send_message.assert_not_awaited()
        bot.send_photo.assert_awaited_once()
