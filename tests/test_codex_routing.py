"""Harness dispatch and session ownership regressions."""

import json
from unittest.mock import patch

import bridge
from patchbay import models, sessions
from patchbay.harness import TurnFinal


def test_legacy_sessions_belong_to_pi(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    sessions.save_session_id("11_22", "pi-thread")
    assert sessions.get_session_id("11_22") == "pi-thread"
    assert sessions.get_session_id("11_22", harness="codex") is None


def test_codex_sessions_are_not_resumed_by_pi(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    sessions.save_session_id("11_22", "codex-thread", harness="codex")
    assert sessions.get_session_id("11_22", harness="codex") == "codex-thread"
    assert sessions.get_session_id("11_22") is None
    assert json.loads((tmp_path / "11_22.json").read_text())["harness"] == "codex"


def test_codex_dispatch_and_model_default(monkeypatch):
    monkeypatch.setattr(models, "CODEX_MODEL", None)
    requests = []

    class FakeCodex:
        def __init__(self, **kwargs):
            assert "proc_setter" in kwargs

        async def run_turn(self, req):
            requests.append(req)
            yield TurnFinal("codex-thread", 1, None, "Codex reply")

    monkeypatch.setattr(bridge, "CodexHarness", FakeCodex, raising=False)
    with (
        patch("bridge.get_session_id", return_value=None),
        patch("bridge.save_session_id") as save,
        patch("bridge.get_chat_harness", return_value="codex"),
        patch("bridge.get_chat_working_dir", return_value="/tmp"),
        patch("bridge.get_chat_agent", return_value=None),
        patch("bridge._load_chat_projects", return_value={}),
        patch("bridge._log_activity"),
    ):
        assert bridge.run_claude("hello", "11_22") == "Codex reply"
        save.assert_called_once_with("11_22", "codex-thread", harness="codex")
    assert requests[0].model is None


def test_inquiry_uses_codex_capabilities(monkeypatch):
    from patchbay.commands import inquiry

    monkeypatch.setattr(models, "CODEX_MODEL", None)
    monkeypatch.setattr(inquiry, "get_chat_harness", lambda key: "codex", raising=False)
    monkeypatch.setattr(inquiry, "get_session_id", lambda *a, **kw: None)
    name, harness, req = inquiry.resolve_harness_for_inquiry("11_22")
    assert name == harness.name == "codex"
    assert req.model is None
    assert not hasattr(harness, "get_context")


async def test_bridge_cancel_calls_harness_cancel():
    from unittest.mock import AsyncMock

    state = bridge.SessionState()
    state.harness = type("H", (), {"cancel": AsyncMock()})()
    await bridge._cancel_session_async(state)
    state.harness.cancel.assert_awaited_once()


async def test_codex_usage_does_not_query_claude(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from patchbay.commands import inquiry, observability

    monkeypatch.setattr(inquiry, "resolve_harness_for_inquiry", lambda key: ("codex", object(), None))
    monkeypatch.setattr(observability, "_ccusage_report", AsyncMock())
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=11), message=SimpleNamespace(message_thread_id=22, reply_text=AsyncMock())
    )
    await observability.cmd_usage(update, None)
    observability._ccusage_report.assert_not_awaited()
    assert "subscription" in update.message.reply_text.call_args.args[0]


async def test_bridge_kill_does_not_classify_codex_as_oom(tmp_path):
    import asyncio
    from patchbay.harness import CodexHarness, TurnError
    from tests.test_harness_codex import fake_codex, request

    state = bridge.SessionState()
    state.worker_loop = asyncio.get_running_loop()
    started = asyncio.Event()

    def on_proc(proc):
        state.proc = proc
        if proc is not None:
            state.worker_loop.call_soon_threadsafe(started.set)

    state.harness = CodexHarness(codex_path=fake_codex(tmp_path, "time.sleep(10)"), proc_setter=on_proc)

    async def collect():
        return [event async for event in state.harness.run_turn(request(tmp_path))]

    turn = asyncio.create_task(collect())
    await asyncio.wait_for(started.wait(), 2)
    await bridge._cancel_session_async(state)
    result = await asyncio.wait_for(turn, 2)
    assert isinstance(result[-1], TurnError)
    assert result[-1].kind == "process_died"
    assert not result[-1].retryable
