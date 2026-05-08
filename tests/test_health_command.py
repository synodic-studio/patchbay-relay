"""Tests for /health — the bridge liveness / observability command."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import bridge


def _make_update():
    u = MagicMock()
    u.effective_chat.id = 100
    u.message.message_thread_id = None
    u.message.reply_text = AsyncMock()
    return u


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    import patchbay.config
    import patchbay.sessions

    # Redirect all path-backed state into a temp dir so tests don't touch
    # real session/pending files.
    monkeypatch.setattr(patchbay.config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(patchbay.config, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(patchbay.config, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(bridge, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(bridge, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(patchbay.sessions, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(patchbay.sessions, "PENDING_DIR", tmp_path / "pending")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "pending").mkdir()
    monkeypatch.setattr(bridge, "_sessions", {})
    # cmd_health (in patchbay.commands.observability) reads
    # BRIDGE_STARTED_AT from patchbay.runtime; patch all aliases.
    import patchbay.commands.observability as _obs_cmd
    import patchbay.runtime
    monkeypatch.setattr(bridge, "_BRIDGE_STARTED_AT", time.time() - 125)
    monkeypatch.setattr(patchbay.runtime, "BRIDGE_STARTED_AT", time.time() - 125)
    monkeypatch.setattr(_obs_cmd, "BRIDGE_STARTED_AT", time.time() - 125)
    return tmp_path


@pytest.mark.asyncio
async def test_health_baseline_no_sessions():
    update = _make_update()
    ctx = MagicMock()
    await bridge.cmd_health(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "bridge /health" in reply
    assert "uptime: 2m" in reply  # 125s elapsed → 2m
    assert "active sessions: 0" in reply
    assert "session files on disk: 0" in reply
    assert "pending messages: 0" in reply
    assert "failed pending (archived): 0" in reply
    assert "disk free:" in reply


@pytest.mark.asyncio
async def test_health_counts_sessions_pending_failed(_isolate):
    # Two session files, three pending files, one failed-pending file.
    (_isolate / "sessions" / "a.json").write_text("{}")
    (_isolate / "sessions" / "b.json").write_text("{}")
    (_isolate / "pending" / "p1.json").write_text("{}")
    (_isolate / "pending" / "p2.json").write_text("{}")
    (_isolate / "pending" / "p3.json").write_text("{}")
    failed = _isolate / "pending" / "failed"
    failed.mkdir()
    (failed / "f1.json").write_text("{}")

    # One active processing session
    bridge._get_session_state("123_5").processing = True

    update = _make_update()
    ctx = MagicMock()
    await bridge.cmd_health(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "active sessions: 1" in reply
    assert "session files on disk: 2" in reply
    assert "pending messages: 3" in reply
    assert "failed pending (archived): 1" in reply
    assert "active keys: 123_5" in reply


@pytest.mark.asyncio
async def test_health_handles_disk_usage_error(monkeypatch, caplog):
    import logging
    import shutil

    def bad_usage(_path):
        raise OSError("device missing")

    monkeypatch.setattr(shutil, "disk_usage", bad_usage)

    update = _make_update()
    ctx = MagicMock()
    with caplog.at_level(logging.WARNING, logger="bridge"):
        await bridge.cmd_health(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "disk free: unknown" in reply
    assert any("disk_usage failed" in r.message for r in caplog.records)
