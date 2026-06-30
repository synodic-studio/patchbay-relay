"""Tests for heartbeat bubble task and projects toggle."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _import_bridge():
    import bridge  # noqa: F401 — ensures telegram_send is fully wired


# --- projects.py toggle ---


def test_heartbeat_default_true():
    from patchbay.projects import get_chat_heartbeat

    assert get_chat_heartbeat("nonexistent:key") is True


def test_heartbeat_set_off_then_on(tmp_path, monkeypatch):
    import patchbay.projects as proj

    monkeypatch.setattr(proj, "CHAT_PROJECTS_FILE", tmp_path / "chat_projects.json")
    monkeypatch.setattr("patchbay.config.CHAT_PROJECTS_FILE", tmp_path / "chat_projects.json")
    proj.set_chat_heartbeat("chat:1", False)
    assert proj.get_chat_heartbeat("chat:1") is False
    proj.set_chat_heartbeat("chat:1", True)
    assert proj.get_chat_heartbeat("chat:1") is True


def test_heartbeat_preserves_other_keys(tmp_path, monkeypatch):
    import patchbay.projects as proj

    monkeypatch.setattr(proj, "CHAT_PROJECTS_FILE", tmp_path / "chat_projects.json")
    monkeypatch.setattr("patchbay.config.CHAT_PROJECTS_FILE", tmp_path / "chat_projects.json")
    proj.set_chat_harness("chat:1", "cc-sdk")
    proj.set_chat_heartbeat("chat:1", False)
    assert proj.get_chat_harness("chat:1") == "cc-sdk"
    assert proj.get_chat_heartbeat("chat:1") is False


# --- _run_heartbeat task ---


@pytest.mark.asyncio
async def test_heartbeat_sends_after_delay():
    from patchbay.telegram_send import _run_heartbeat

    bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 77
    bot.send_message = AsyncMock(return_value=sent_msg)
    bot.edit_message_text = AsyncMock()

    stop = asyncio.Event()
    holder: list[int] = []
    start = time.time()

    task = asyncio.create_task(_run_heartbeat(100, None, stop, bot, start, holder, delay=0.05, interval=10.0))
    await asyncio.sleep(0.12)
    stop.set()
    await task

    bot.send_message.assert_called_once()
    assert holder == [77]
    call_text = bot.send_message.call_args.kwargs.get("text", "")
    assert "⏳ Working" in call_text


@pytest.mark.asyncio
async def test_heartbeat_stops_before_delay():
    from patchbay.telegram_send import _run_heartbeat

    bot = MagicMock()
    bot.send_message = AsyncMock()

    stop = asyncio.Event()
    holder: list[int] = []

    stop.set()  # already stopped
    await _run_heartbeat(100, None, stop, bot, time.time(), holder, delay=10.0, interval=10.0)

    bot.send_message.assert_not_called()
    assert holder == []


@pytest.mark.asyncio
async def test_heartbeat_edits_on_interval():
    from patchbay.telegram_send import _run_heartbeat

    bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 55
    bot.send_message = AsyncMock(return_value=sent_msg)
    bot.edit_message_text = AsyncMock()

    stop = asyncio.Event()
    holder: list[int] = []

    task = asyncio.create_task(_run_heartbeat(100, None, stop, bot, time.time(), holder, delay=0.02, interval=0.05))
    await asyncio.sleep(0.18)
    stop.set()
    await task

    assert bot.send_message.call_count == 1
    assert bot.edit_message_text.call_count >= 1


@pytest.mark.asyncio
async def test_heartbeat_send_failure_is_silent():
    from patchbay.telegram_send import _run_heartbeat

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=Exception("network error"))

    stop = asyncio.Event()
    holder: list[int] = []

    # Should complete without raising
    await _run_heartbeat(100, None, stop, bot, time.time(), holder, delay=0.01, interval=10.0)
    assert holder == []


@pytest.mark.asyncio
async def test_heartbeat_uses_thread_id():
    from patchbay.telegram_send import _run_heartbeat

    bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 1
    bot.send_message = AsyncMock(return_value=sent_msg)

    stop = asyncio.Event()
    holder: list[int] = []

    task = asyncio.create_task(_run_heartbeat(100, 42, stop, bot, time.time(), holder, delay=0.02, interval=10.0))
    await asyncio.sleep(0.06)
    stop.set()
    await task

    call_kwargs = bot.send_message.call_args.kwargs
    assert call_kwargs.get("message_thread_id") == 42
