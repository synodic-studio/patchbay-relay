"""Tests for /activity Telegram command — surfaces recent activity.jsonl
entries from the user's phone, with optional event filter and count."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import bridge


@pytest.fixture
def isolated_activity_log(tmp_path, monkeypatch):
    log = tmp_path / "activity.jsonl"
    # cmd_activity reads bridge.ACTIVITY_LOG directly (the import-time alias).
    monkeypatch.setattr(bridge, "ACTIVITY_LOG", log)
    return log


def _write(log_path, events: list[dict]) -> None:
    with open(log_path, "a") as f:
        for e in events:
            e.setdefault("ts", time.time())
            f.write(json.dumps(e) + "\n")


def _make_update(text: str = "/activity"):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


class TestCmdActivity:
    @pytest.mark.asyncio
    async def test_no_log_yet(self, isolated_activity_log):
        update = _make_update()
        await bridge.cmd_activity(update, MagicMock())
        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "does not exist" in reply

    @pytest.mark.asyncio
    async def test_returns_recent_entries(self, isolated_activity_log):
        _write(
            isolated_activity_log,
            [
                {"event": "turn_invoke", "session_key": "k1"},
                {"event": "turn_complete", "session_key": "k1", "turns_used": 3},
            ],
        )
        update = _make_update("/activity")
        await bridge.cmd_activity(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "turn_invoke" in reply
        assert "turn_complete" in reply
        assert "turns_used=3" in reply

    @pytest.mark.asyncio
    async def test_event_filter(self, isolated_activity_log):
        _write(
            isolated_activity_log,
            [
                {"event": "message", "session_key": "k1"},
                {"event": "self_heal", "kind": "claude_oom_137", "fixed": True},
                {"event": "message", "session_key": "k2"},
            ],
        )
        update = _make_update("/activity self_heal")
        await bridge.cmd_activity(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "self_heal" in reply
        assert "kind=claude_oom_137" in reply
        assert "fixed=True" in reply
        # Other events should be filtered out
        assert "message" not in reply or reply.count("message") == 0

    @pytest.mark.asyncio
    async def test_count_argument(self, isolated_activity_log):
        # Write 10 entries, request only 3
        events = [{"event": f"evt_{i}"} for i in range(10)]
        _write(isolated_activity_log, events)
        update = _make_update("/activity evt 3")
        await bridge.cmd_activity(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        # The header shows count
        assert "last 3" in reply
        # Newest first ordering → the latest 3 should appear
        assert "evt_9" in reply
        assert "evt_8" in reply
        assert "evt_7" in reply
        assert "evt_6" not in reply

    @pytest.mark.asyncio
    async def test_no_match_returns_friendly_message(self, isolated_activity_log):
        _write(isolated_activity_log, [{"event": "message"}])
        update = _make_update("/activity does_not_exist_kind")
        await bridge.cmd_activity(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "No activity entries" in reply

    @pytest.mark.asyncio
    async def test_malformed_lines_skipped(self, isolated_activity_log):
        with open(isolated_activity_log, "w") as f:
            f.write('{"event": "good", "ts": 1}\n')
            f.write("not json at all\n")
            f.write('{"event": "also_good", "ts": 2}\n')
        update = _make_update("/activity")
        await bridge.cmd_activity(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "good" in reply
        assert "also_good" in reply
        # No exception bubbled up

    @pytest.mark.asyncio
    async def test_count_capped_at_25(self, isolated_activity_log):
        _write(isolated_activity_log, [{"event": "x"} for _ in range(50)])
        update = _make_update("/activity x 999")
        await bridge.cmd_activity(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        # max cap is 25
        assert "last 25" in reply

    @pytest.mark.asyncio
    async def test_invalid_count_falls_back_to_default(self, isolated_activity_log):
        _write(isolated_activity_log, [{"event": "x"} for _ in range(20)])
        update = _make_update("/activity x notanumber")
        await bridge.cmd_activity(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        # Default is 8
        assert "last 8" in reply

    @pytest.mark.asyncio
    async def test_long_field_value_truncated(self, isolated_activity_log):
        _write(
            isolated_activity_log,
            [{"event": "markdown_send_failed", "error": "x" * 200}],
        )
        update = _make_update("/activity markdown")
        await bridge.cmd_activity(update, MagicMock())
        reply = update.message.reply_text.call_args[0][0]
        assert "…" in reply  # truncation marker
