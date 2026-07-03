"""Tests for `_cancel_session_async` — subprocess cancel (pi) via SIGKILL.

Subprocess harnesses (pi) cancel via `state.proc.kill()` (sync, immediate).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import bridge


@pytest.fixture(autouse=True)
def _isolate_sessions(monkeypatch):
    monkeypatch.setattr(bridge, "_sessions", {})


# ---------------------------------------------------------------------------
# Subprocess path: `state.proc` set, `_cancel_session_async` SIGKILLs the proc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_path_kills_proc():
    state = bridge.SessionState()
    state.proc = MagicMock()
    state.proc.pid = 12345

    await bridge._cancel_session_async(state)

    state.proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_subprocess_path_swallows_oserror_on_kill():
    state = bridge.SessionState()
    state.proc = MagicMock()
    state.proc.kill.side_effect = OSError("already gone")

    # Must not raise.
    await bridge._cancel_session_async(state)


@pytest.mark.asyncio
async def test_no_proc_is_noop():
    """Calling cancel on a fully-empty state does nothing and doesn't raise."""
    state = bridge.SessionState()
    await bridge._cancel_session_async(state)


# ---------------------------------------------------------------------------
# _iter_active_sessions: proc-based snapshot
# ---------------------------------------------------------------------------


def test_iter_active_sessions_includes_proc_only_state():
    state = bridge.SessionState()
    state.proc = MagicMock()
    bridge._sessions["a"] = state
    pairs = bridge._iter_active_sessions()
    assert [k for k, _ in pairs] == ["a"]


def test_iter_active_sessions_skips_idle_state():
    bridge._sessions["c"] = bridge.SessionState()
    pairs = bridge._iter_active_sessions()
    assert pairs == []
