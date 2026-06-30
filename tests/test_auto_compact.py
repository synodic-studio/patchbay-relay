"""Tests for _auto_compact_if_needed in bridge.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge
from patchbay.harness.base import CompactResult, ContextUsage


def _make_usage(pct: float, used: int = 50000, max_t: int = 100000) -> ContextUsage:
    return ContextUsage(used_tokens=used, max_tokens=max_t, percentage=pct)


@pytest.mark.asyncio
async def test_no_compact_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "AUTO_COMPACT_PCT", None)
    monkeypatch.setattr(bridge, "AUTO_COMPACT_TOKENS", None)
    bot = MagicMock()
    # Should return immediately without any harness work
    await bridge._auto_compact_if_needed(bot, 1, None, "key:1")
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_no_compact_without_session(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "AUTO_COMPACT_PCT", 80.0)
    monkeypatch.setattr(bridge, "AUTO_COMPACT_TOKENS", None)
    with patch("bridge.get_session_id", return_value=None):
        bot = MagicMock()
        await bridge._auto_compact_if_needed(bot, 1, None, "key:1")
        bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_no_compact_for_pi_harness(monkeypatch):
    monkeypatch.setattr(bridge, "AUTO_COMPACT_PCT", 80.0)
    monkeypatch.setattr(bridge, "AUTO_COMPACT_TOKENS", None)
    with (
        patch("bridge.get_session_id", return_value="sess-abc"),
        patch("bridge.get_chat_harness", return_value="pi"),
    ):
        bot = MagicMock()
        await bridge._auto_compact_if_needed(bot, 1, None, "key:1")
        bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_compact_triggered_by_pct(monkeypatch):
    monkeypatch.setattr(bridge, "AUTO_COMPACT_PCT", 80.0)
    monkeypatch.setattr(bridge, "AUTO_COMPACT_TOKENS", None)

    mock_harness = MagicMock()
    mock_harness.get_context = AsyncMock(return_value=_make_usage(85.0))
    mock_harness.compact = AsyncMock(return_value=CompactResult(succeeded=True, message="50k → 10k"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    with (
        patch("bridge.get_session_id", return_value="sess-abc"),
        patch("bridge.get_chat_harness", return_value="cc-sdk"),
        patch("bridge.get_chat_working_dir", return_value="/tmp"),
        patch("bridge.ClaudeSdkHarness", return_value=mock_harness),
    ):
        with (
            patch("bridge.get_chat_model", return_value=None),
            patch("bridge.resolve_effort", return_value="medium"),
        ):
            await bridge._auto_compact_if_needed(bot, 100, None, "key:1")

    mock_harness.compact.assert_called_once()
    bot.send_message.assert_called_once()
    text = bot.send_message.call_args.kwargs.get("text", "")
    assert "Auto-compacted" in text
    assert "85%" in text


@pytest.mark.asyncio
async def test_compact_triggered_by_tokens(monkeypatch):
    monkeypatch.setattr(bridge, "AUTO_COMPACT_PCT", None)
    monkeypatch.setattr(bridge, "AUTO_COMPACT_TOKENS", 100000)

    mock_harness = MagicMock()
    mock_harness.get_context = AsyncMock(return_value=_make_usage(60.0, used=150000))
    mock_harness.compact = AsyncMock(return_value=CompactResult(succeeded=True, message="150k → 20k"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    with (
        patch("bridge.get_session_id", return_value="sess-abc"),
        patch("bridge.get_chat_harness", return_value="cc-sdk"),
        patch("bridge.get_chat_working_dir", return_value="/tmp"),
        patch("bridge.ClaudeSdkHarness", return_value=mock_harness),
        patch("bridge.get_chat_model", return_value=None),
        patch("bridge.resolve_effort", return_value="medium"),
    ):
        await bridge._auto_compact_if_needed(bot, 100, None, "key:1")

    mock_harness.compact.assert_called_once()
    bot.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_no_compact_below_threshold(monkeypatch):
    monkeypatch.setattr(bridge, "AUTO_COMPACT_PCT", 90.0)
    monkeypatch.setattr(bridge, "AUTO_COMPACT_TOKENS", None)

    mock_harness = MagicMock()
    mock_harness.get_context = AsyncMock(return_value=_make_usage(70.0))
    mock_harness.compact = AsyncMock()

    bot = MagicMock()
    bot.send_message = AsyncMock()

    with (
        patch("bridge.get_session_id", return_value="sess-abc"),
        patch("bridge.get_chat_harness", return_value="cc-sdk"),
        patch("bridge.get_chat_working_dir", return_value="/tmp"),
        patch("bridge.ClaudeSdkHarness", return_value=mock_harness),
        patch("bridge.get_chat_model", return_value=None),
        patch("bridge.resolve_effort", return_value="medium"),
    ):
        await bridge._auto_compact_if_needed(bot, 100, None, "key:1")

    mock_harness.compact.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_compact_error_swallowed(monkeypatch):
    monkeypatch.setattr(bridge, "AUTO_COMPACT_PCT", 80.0)
    monkeypatch.setattr(bridge, "AUTO_COMPACT_TOKENS", None)

    mock_harness = MagicMock()
    mock_harness.get_context = AsyncMock(return_value=_make_usage(85.0))
    mock_harness.compact = AsyncMock(side_effect=RuntimeError("SDK exploded"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    with (
        patch("bridge.get_session_id", return_value="sess-abc"),
        patch("bridge.get_chat_harness", return_value="cc-sdk"),
        patch("bridge.get_chat_working_dir", return_value="/tmp"),
        patch("bridge.ClaudeSdkHarness", return_value=mock_harness),
        patch("bridge.get_chat_model", return_value=None),
        patch("bridge.resolve_effort", return_value="medium"),
    ):
        # Should not raise
        await bridge._auto_compact_if_needed(bot, 100, None, "key:1")

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_cc_sdk_mop_uses_sdk_harness(monkeypatch):
    monkeypatch.setattr(bridge, "AUTO_COMPACT_PCT", 80.0)
    monkeypatch.setattr(bridge, "AUTO_COMPACT_TOKENS", None)

    mock_harness = MagicMock()
    mock_harness.get_context = AsyncMock(return_value=_make_usage(82.0))
    mock_harness.compact = AsyncMock(return_value=CompactResult(succeeded=True, message="done"))

    bot = MagicMock()
    bot.send_message = AsyncMock()

    with (
        patch("bridge.get_session_id", return_value="sess-abc"),
        patch("bridge.get_chat_harness", return_value="cc-sdk-mop"),
        patch("bridge.get_chat_working_dir", return_value="/tmp"),
        patch("bridge.ClaudeSdkHarness", return_value=mock_harness),
        patch("bridge.get_chat_model", return_value=None),
        patch("bridge.resolve_effort", return_value="medium"),
    ):
        await bridge._auto_compact_if_needed(bot, 100, None, "key:1")

    # cc-sdk-mop should still trigger compact via the ClaudeSdkHarness path
    mock_harness.compact.assert_called_once()
