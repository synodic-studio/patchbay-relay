"""Tests for the /harness Telegram command + harness=field on activity logs.

Phase 1c of. /harness writes through `set_chat_harness` and reads
through `get_chat_harness`; run_claude reads the per-chat selection (or
`DEFAULT_HARNESS`) and tags every activity entry with `harness=...`.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge
import stargate.projects


SESSION_KEY = "11_22"
MESSAGE = "hi"


def _make_update(text: str | None, chat_id: int = 11, thread_id: int = 22):
    """Minimal Update / Context stand-in for cmd_harness."""
    msg = SimpleNamespace(
        text=text,
        message_thread_id=thread_id,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        message=msg,
        effective_chat=SimpleNamespace(id=chat_id),
    )
    args = (text.split()[1:] if text else [])
    context = SimpleNamespace(args=args, bot=MagicMock())
    return update, context


@pytest.fixture(autouse=True)
def _isolate_projects(tmp_path, monkeypatch):
    projects_file = tmp_path / "chat_projects.json"
    monkeypatch.setattr(stargate.projects, "CHAT_PROJECTS_FILE", projects_file)


# ---------------------------------------------------------------------------
# /harness command
# ---------------------------------------------------------------------------


class TestCmdHarness:
    @pytest.mark.asyncio
    async def test_no_args_shows_default_when_unset(self):
        update, context = _make_update("/harness")
        await bridge.cmd_harness(update, context)
        sent = update.message.reply_text.call_args[0][0]
        assert "default" in sent
        assert bridge.DEFAULT_HARNESS in sent

    @pytest.mark.asyncio
    async def test_set_cc_cli_writes_to_projects(self):
        update, context = _make_update("/harness cc-cli")
        await bridge.cmd_harness(update, context)
        assert stargate.projects.get_chat_harness(SESSION_KEY) == "cc-cli"
        sent = update.message.reply_text.call_args[0][0]
        assert "cc-cli" in sent

    @pytest.mark.asyncio
    async def test_set_cc_sdk_stores_value(self):
        update, context = _make_update("/harness cc-sdk")
        await bridge.cmd_harness(update, context)
        assert stargate.projects.get_chat_harness(SESSION_KEY) == "cc-sdk"
        sent = update.message.reply_text.call_args[0][0]
        assert "cc-sdk" in sent

    @pytest.mark.asyncio
    async def test_default_clears_override(self):
        stargate.projects.set_chat_harness(SESSION_KEY, "cc-sdk")
        update, context = _make_update("/harness default")
        await bridge.cmd_harness(update, context)
        assert stargate.projects.get_chat_harness(SESSION_KEY) is None
        sent = update.message.reply_text.call_args[0][0]
        assert "default" in sent

    @pytest.mark.asyncio
    async def test_invalid_value_rejected_without_writing(self):
        update, context = _make_update("/harness garbage")
        await bridge.cmd_harness(update, context)
        assert stargate.projects.get_chat_harness(SESSION_KEY) is None
        sent = update.message.reply_text.call_args[0][0]
        assert "Invalid" in sent


# ---------------------------------------------------------------------------
# harness= on activity entries
# ---------------------------------------------------------------------------


def _make_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock(spec=subprocess.Popen)
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 99999
    return proc


def _valid_json_stdout(text="hello", session_id="sess-act-1"):
    return json.dumps(
        [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
            {"type": "result", "session_id": session_id, "result": text},
        ]
    )


@pytest.fixture
def _bridge_run_claude_deps(monkeypatch):
    """Stub out everything run_claude needs except _log_activity."""
    monkeypatch.setattr(bridge, "_sessions", {})

    def _fake_drain_streams(self, proc, timeout):
        return proc.communicate(timeout=timeout)

    with (
        patch("bridge.get_session_id", return_value=None),
        patch("bridge.clear_session"),
        patch("bridge.save_session_id"),
        patch("bridge.get_chat_working_dir", return_value="/tmp/fake"),
        patch("bridge.get_chat_agent", return_value=None),
        patch("bridge._load_chat_projects", return_value={}),
        patch("bridge._parse_project_entry", return_value=(None, None)),
        patch(
            "stargate.harness.claude_cli.ClaudeCliHarness._drain_streams",
            _fake_drain_streams,
        ),
    ):
        yield


class TestHarnessActivityField:
    def test_default_harness_lands_on_invoke_and_complete(
        self, _bridge_run_claude_deps
    ):
        proc = _make_proc(stdout=_valid_json_stdout())
        events = []

        def _capture(event, **kwargs):
            events.append({"event": event, **kwargs})

        with (
            patch("bridge.subprocess.Popen", return_value=proc),
            patch("bridge._log_activity", side_effect=_capture),
            patch("bridge.get_chat_harness", return_value=None),
        ):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        # Both claude_invoke and claude_complete carry the harness field.
        invoke = next(e for e in events if e["event"] == "claude_invoke")
        complete = next(e for e in events if e["event"] == "claude_complete")
        assert invoke["harness"] == "cc-cli"
        assert invoke["harness_requested"] == bridge.DEFAULT_HARNESS
        assert complete["harness"] == "cc-cli"

    def test_per_chat_cc_sdk_dispatches_to_sdk_harness(
        self, _bridge_run_claude_deps
    ):
        """Phase 3b: when the per-chat selection is cc-sdk, run_claude
        instantiates `ClaudeSdkHarness` (not ClaudeCliHarness) and the
        activity log records `harness=cc-sdk`."""
        from stargate.harness import TextDelta, TurnFinal

        events = []

        def _capture(event, **kwargs):
            events.append({"event": event, **kwargs})

        # Fake harness records that ClaudeSdkHarness was instantiated
        # and yields a minimal successful stream.
        instantiated = {"called": False}

        class _FakeSdkHarness:
            name = "cc-sdk"

            def __init__(self, **kwargs):
                instantiated["called"] = True
                instantiated["kwargs"] = kwargs

            async def run_turn(self, req):
                yield TextDelta(text="hello from sdk", final=True)
                yield TurnFinal(
                    session_id="sess-sdk-1",
                    num_turns=1,
                    total_cost_usd=0.001,
                    raw_text="hello from sdk",
                )

            async def cancel(self) -> None:
                pass

        with (
            patch("bridge.ClaudeSdkHarness", _FakeSdkHarness),
            patch("bridge._log_activity", side_effect=_capture),
            patch("bridge.get_chat_harness", return_value="cc-sdk"),
        ):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert instantiated["called"] is True
        # on_progress was wired through (so the stall detector keeps working)
        assert "on_progress" in instantiated["kwargs"]
        assert result == "hello from sdk"

        invoke = next(e for e in events if e["event"] == "claude_invoke")
        complete = next(e for e in events if e["event"] == "claude_complete")
        assert invoke["harness"] == "cc-sdk"
        assert invoke["harness_requested"] == "cc-sdk"
        assert complete["harness"] == "cc-sdk"
