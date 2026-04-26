"""Tests for `_cancel_session_async` — the harness-agnostic cancel helper.

cc-cli sessions cancel via `state.proc.kill()` (sync, immediate). cc-sdk
sessions cancel via `run_coroutine_threadsafe(state.harness.cancel(),
state.worker_loop)` because the harness's task lives in a separate event
loop on a worker thread.

Phase 3a of.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

import bridge


@pytest.fixture(autouse=True)
def _isolate_sessions(monkeypatch):
    monkeypatch.setattr(bridge, "_sessions", {})


# ---------------------------------------------------------------------------
# cc-cli path: `state.proc` set, `_cancel_session_async` SIGKILLs the proc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc_cli_path_kills_proc():
    state = bridge.SessionState()
    state.proc = MagicMock()
    state.proc.pid = 12345

    await bridge._cancel_session_async(state)

    state.proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_cc_cli_path_swallows_oserror_on_kill():
    state = bridge.SessionState()
    state.proc = MagicMock()
    state.proc.kill.side_effect = OSError("already gone")

    # Must not raise.
    await bridge._cancel_session_async(state)


# ---------------------------------------------------------------------------
# cc-sdk path: no proc, harness + worker_loop set, cancel scheduled across loops
# ---------------------------------------------------------------------------


def _spin_up_worker_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Run an asyncio loop on a worker thread, return both for use + teardown."""
    ready = threading.Event()
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder["loop"] = loop
        ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    ready.wait(timeout=2.0)
    return loop_holder["loop"], thread


def _stop_worker_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_cc_sdk_path_schedules_cancel_on_worker_loop():
    """Harness.cancel() runs on the worker loop, not the test's loop."""
    worker_loop, worker_thread = _spin_up_worker_loop()
    try:
        cancel_called_on: dict[str, asyncio.AbstractEventLoop | None] = {"loop": None}

        class _FakeHarness:
            name = "cc-sdk"

            async def cancel(self) -> None:
                cancel_called_on["loop"] = asyncio.get_running_loop()

        state = bridge.SessionState()
        state.harness = _FakeHarness()
        state.worker_loop = worker_loop

        await bridge._cancel_session_async(state)

        assert cancel_called_on["loop"] is worker_loop
    finally:
        _stop_worker_loop(worker_loop, worker_thread)


@pytest.mark.asyncio
async def test_cc_sdk_path_times_out_on_wedged_cancel():
    """If cancel hangs forever, _cancel_session_async returns within ~5s."""
    worker_loop, worker_thread = _spin_up_worker_loop()
    try:

        class _WedgedHarness:
            name = "cc-sdk"

            async def cancel(self) -> None:
                await asyncio.sleep(60)  # well past the 5s timeout

        state = bridge.SessionState()
        state.harness = _WedgedHarness()
        state.worker_loop = worker_loop

        # Don't actually wait the full timeout in CI — patch it via a short
        # asyncio.wait_for-style cap. We assert the call returns *and* doesn't
        # raise, by capping the test itself with a tighter timeout.
        await asyncio.wait_for(bridge._cancel_session_async(state), timeout=10.0)
    finally:
        _stop_worker_loop(worker_loop, worker_thread)


@pytest.mark.asyncio
async def test_no_harness_no_proc_is_noop():
    """Calling cancel on a fully-empty state does nothing and doesn't raise."""
    state = bridge.SessionState()
    await bridge._cancel_session_async(state)


@pytest.mark.asyncio
async def test_cc_cli_takes_precedence_over_cc_sdk():
    """If both proc and harness are set (cc-cli mid-run), proc.kill() wins
    and we don't try to schedule across loops."""
    worker_loop, worker_thread = _spin_up_worker_loop()
    try:
        sdk_cancel_called = False

        class _ShouldNotCancel:
            name = "cc-cli"

            async def cancel(self) -> None:
                nonlocal sdk_cancel_called
                sdk_cancel_called = True

        state = bridge.SessionState()
        state.proc = MagicMock()
        state.harness = _ShouldNotCancel()
        state.worker_loop = worker_loop

        await bridge._cancel_session_async(state)

        state.proc.kill.assert_called_once()
        assert sdk_cancel_called is False
    finally:
        _stop_worker_loop(worker_loop, worker_thread)


# ---------------------------------------------------------------------------
# _iter_active_sessions: backend-agnostic snapshot
# ---------------------------------------------------------------------------


def test_iter_active_sessions_includes_proc_only_state():
    state = bridge.SessionState()
    state.proc = MagicMock()
    bridge._sessions["a"] = state
    pairs = bridge._iter_active_sessions()
    assert [k for k, _ in pairs] == ["a"]


def test_iter_active_sessions_includes_harness_only_state():
    state = bridge.SessionState()
    state.harness = MagicMock()
    bridge._sessions["b"] = state
    pairs = bridge._iter_active_sessions()
    assert [k for k, _ in pairs] == ["b"]


def test_iter_active_sessions_skips_idle_state():
    bridge._sessions["c"] = bridge.SessionState()
    pairs = bridge._iter_active_sessions()
    assert pairs == []


# ---------------------------------------------------------------------------
# /kill end-to-end on a cc-sdk session: harness.cancel runs on the worker loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_kill_cancels_cc_sdk_harness_across_loops():
    """Simulate an in-flight cc-sdk turn, /kill it, confirm harness.cancel
    ran on the worker loop (not the main loop)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    worker_loop, worker_thread = _spin_up_worker_loop()
    try:
        cancel_called_on: dict[str, asyncio.AbstractEventLoop | None] = {"loop": None}

        class _SdkLikeHarness:
            name = "cc-sdk"

            async def cancel(self) -> None:
                cancel_called_on["loop"] = asyncio.get_running_loop()

        # Set up the session as if a cc-sdk turn is in flight.
        state = bridge._get_session_state("99_88")
        state.harness = _SdkLikeHarness()
        state.worker_loop = worker_loop
        # state.proc stays None — that's the cc-sdk shape.

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=99),
            message=SimpleNamespace(
                message_thread_id=88,
                reply_text=AsyncMock(),
            ),
        )
        context = SimpleNamespace()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bridge, "_log_activity", lambda *a, **kw: None)
            await bridge.cmd_kill(update, context)

        assert cancel_called_on["loop"] is worker_loop
        # User saw the killed message, not "no active process"
        sent = update.message.reply_text.call_args[0][0]
        assert "Killed" in sent
    finally:
        _stop_worker_loop(worker_loop, worker_thread)
