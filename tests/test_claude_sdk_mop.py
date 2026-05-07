"""Tests for ClaudeSdkMopHarness — MOP output filter wrapper.

Stubs out the inner ClaudeSdkHarness so tests don't touch the claude CLI.
Verifies: buffering, pass-through on Accept, violation logging on Reject,
configurable LLM backend stub, enforce mode retry loop, edit/rewrite,
empty-message structural check, and that TurnError from inner harness
passes through without MOP evaluation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from patchbay.harness.base import (
    TextDelta,
    ToolUse,
    TurnError,
    TurnFinal,
    TurnRequest,
)
from patchbay.harness.claude_sdk_mop import ClaudeSdkMopHarness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(session_key: str = "test_session") -> TurnRequest:
    return TurnRequest(
        prompt="test",
        session_key=session_key,
        project_dir=Path("/tmp"),
        system_prompt=None,
        resume_session_id=None,
        model=None,
        effort=None,
        allowed_tools=None,
        disallowed_tools=None,
        max_turns=10,
        plugin_dir=None,
    )


async def _events(*items) -> AsyncIterator:
    for item in items:
        yield item


def _make_harness(events_seq, *, llm_backend: str = "stub") -> ClaudeSdkMopHarness:
    harness = ClaudeSdkMopHarness(llm_backend=llm_backend)
    inner = MagicMock()
    inner.run_turn = MagicMock(return_value=_events(*events_seq))
    harness._inner = inner
    return harness


def _make_harness_multi(calls: list[list], *, llm_backend: str = "stub") -> ClaudeSdkMopHarness:
    """Harness whose inner.run_turn returns a different event sequence each call."""
    harness = ClaudeSdkMopHarness(llm_backend=llm_backend)
    iterators = [_events(*seq) for seq in calls]
    side_effect = iter(iterators)
    inner = MagicMock()
    inner.run_turn = MagicMock(side_effect=lambda _req: next(side_effect))
    harness._inner = inner
    return harness


async def _collect(harness: ClaudeSdkMopHarness, req: TurnRequest) -> list:
    results = []
    async for event in harness.run_turn(req):
        results.append(event)
    return results


# ---------------------------------------------------------------------------
# Passthrough mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_passthrough_mode_bypasses_all_eval(monkeypatch):
    """passthrough mode delivers all events without evaluation."""
    monkeypatch.setenv("MOP_MODE", "passthrough")
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="")
    harness = _make_harness([TextDelta(text="x"), final])
    events = await _collect(harness, _req())
    assert len(events) == 2
    assert isinstance(events[-1], TurnFinal)


# ---------------------------------------------------------------------------
# Audit mode (default)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accept_passes_all_events_through(monkeypatch):
    """Non-violating turn: all events including TurnFinal reach the caller."""
    monkeypatch.setenv("MOP_MODE", "audit")
    final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=0.001, raw_text="Done.")
    harness = _make_harness([TextDelta(text="Do"), TextDelta(text="ne."), final])
    events = await _collect(harness, _req())
    assert len(events) == 3
    assert events[-1] is final


@pytest.mark.asyncio
async def test_tool_use_events_pass_through(monkeypatch):
    """ToolUse events buffer and replay before TurnFinal."""
    monkeypatch.setenv("MOP_MODE", "audit")
    tool = ToolUse(name="Read", input={"file_path": "/foo"}, id="t1")
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="read it")
    harness = _make_harness([tool, final])
    events = await _collect(harness, _req())
    assert events[0] is tool
    assert events[1] is final


@pytest.mark.asyncio
async def test_turn_error_passes_through_without_mop(monkeypatch):
    """If inner harness errors, MOP does not evaluate — error passes through."""
    monkeypatch.setenv("MOP_MODE", "audit")
    err = TurnError(kind="timeout", message="timed out", retryable=True, metadata={})
    harness = _make_harness([err])
    events = await _collect(harness, _req())
    assert len(events) == 1
    assert isinstance(events[0], TurnError)
    assert events[0].kind == "timeout"


@pytest.mark.asyncio
async def test_no_violation_no_log(tmp_path, monkeypatch):
    """Audit mode: clean turn does not create the violation log file."""
    monkeypatch.setenv("MOP_MODE", "audit")
    log_file = str(tmp_path / "violations.jsonl")
    monkeypatch.setenv("MOP_LOG_PATH", log_file)
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="Here is the result.")
    harness = _make_harness([final])
    await _collect(harness, _req())
    assert not Path(log_file).exists()


# ---------------------------------------------------------------------------
# Enforce mode — structural empty-message check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enforce_empty_message_retries(monkeypatch, tmp_path):
    """Enforce mode: empty raw_text triggers reject + retry (built-in empty check in mop.filter)."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    monkeypatch.setenv("MOP_MAX_RETRIES", "2")
    monkeypatch.setenv("MOP_RULES_DIR", str(tmp_path))  # empty dir — no yaml rules

    empty_final = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="")
    good_final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="Here you go.")

    harness = _make_harness_multi([[empty_final], [good_final]])
    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    assert events[0].raw_text == "Here you go."


@pytest.mark.asyncio
async def test_enforce_empty_message_max_retries_delivers_last(monkeypatch, tmp_path):
    """Enforce mode: max retries exhausted on empty → deliver last turn anyway."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    monkeypatch.setenv("MOP_MAX_RETRIES", "2")
    monkeypatch.setenv("MOP_RULES_DIR", str(tmp_path))

    empty1 = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="")
    empty2 = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="   ")

    harness = _make_harness_multi([[empty1], [empty2]])
    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    assert events[0].raw_text == "   "


# ---------------------------------------------------------------------------
# v2 build_options() — in-process MCP + Stop hook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v2_harness_constructs_mcp_and_registers_stop_hook(monkeypatch):
    """v2 cc-sdk-mop builds an in-process MCP and a Stop hook callback."""
    from patchbay.harness.claude_sdk_mop import ClaudeSdkMopHarness
    from mop import MOP

    monkeypatch.setenv("MOP_ANTHROPIC_API_KEY", "fake-key")

    h = ClaudeSdkMopHarness()
    # The harness exposes a build_options() that constructs the per-turn MOP +
    # in-process MCP server config + Stop hook callback closing over MOP state.
    options, mop_instance = h.build_options(
        bot=MagicMock(),
        chat_id=42,
        thread_id=None,
        main_loop=asyncio.get_event_loop(),
        rules_dir=None,
    )
    assert isinstance(mop_instance, MOP)
    assert "mop" in options.mcp_servers
    # Stop hook is registered.
    from claude_agent_sdk.types import HookEvent
    assert "Stop" in options.hooks


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_cc_sdk_mop_registered():
    from patchbay.config import VALID_HARNESSES
    from patchbay.harness import CAPABILITIES_BY_NAME
    assert "cc-sdk-mop" in VALID_HARNESSES
    assert "cc-sdk-mop" in CAPABILITIES_BY_NAME


def test_cc_sdk_mop_capabilities():
    from patchbay.harness import CAPABILITIES_BY_NAME
    caps = CAPABILITIES_BY_NAME["cc-sdk-mop"]
    assert caps.supports_resume is True
    assert caps.supports_interrupt is True
    assert caps.supports_inflight_push is False


# ---------------------------------------------------------------------------
# T13: run_claude routes cc-sdk-mop through v2 build_options
# ---------------------------------------------------------------------------

def test_run_claude_cc_sdk_mop_v2_uses_build_options(monkeypatch, tmp_path):
    """run_claude with effective_harness=cc-sdk-mop calls harness.build_options
    and instantiates ClaudeSDKClient with the returned options. The MOP
    instance is held on SessionState.mop so its Stop hook closure stays alive
    for the lifetime of the SDK client.
    """
    import bridge

    # Force the cc-sdk-mop dispatch.
    monkeypatch.setattr(bridge, "get_chat_harness", lambda key: "cc-sdk-mop")
    monkeypatch.setattr(bridge, "get_chat_working_dir", lambda key: str(tmp_path))
    monkeypatch.setattr(bridge, "get_chat_agent", lambda key: None)
    monkeypatch.setattr(bridge, "get_session_id", lambda key: None)
    monkeypatch.setattr(bridge, "save_session_id", lambda key, sid: None)
    monkeypatch.setattr(bridge, "_load_chat_projects", lambda: {})

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()
    monkeypatch.setattr(bridge, "_bot_instance", fake_bot)

    # Provide a sentinel main_loop so the v2 dispatch's None-guard passes.
    fake_loop = MagicMock(name="main_loop")
    monkeypatch.setattr(bridge, "_main_loop", fake_loop)

    # Capture build_options call args + return synthetic options/mop.
    sentinel_options = MagicMock(name="ClaudeAgentOptions")
    sentinel_mop = MagicMock(name="MOP")
    build_options_calls = []

    def fake_build_options(self, *, bot, chat_id, thread_id, main_loop, rules_dir):
        build_options_calls.append(
            {
                "bot": bot,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "main_loop": main_loop,
                "rules_dir": rules_dir,
            }
        )
        return sentinel_options, sentinel_mop

    from patchbay.harness import claude_sdk_mop as mop_mod

    monkeypatch.setattr(
        mop_mod.ClaudeSdkMopHarness, "build_options", fake_build_options, raising=True
    )

    # Fake ClaudeSDKClient — async context manager that records the options
    # it was constructed with and immediately drains. Captures state.mop
    # while the SDK client is alive (before run_claude's finally clears it).
    construct_calls = []
    mop_during_session: list = []

    class FakeResultMessage:
        session_id = "sid-123"
        num_turns = 1
        total_cost_usd = None
        result = ""

    class FakeClient:
        def __init__(self, options):
            construct_calls.append(options)
            self.options = options

        async def __aenter__(self):
            # Snapshot mop pin while the SDK session is open.
            st = bridge._sessions.get("12345_67")
            mop_during_session.append(st.mop if st else None)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def query(self, prompt):
            self.last_prompt = prompt

        async def receive_response(self):
            yield FakeResultMessage()

    # Patch the symbol that build_options'd code path imports. We import lazily
    # in the bridge dispatch, so patch it on claude_agent_sdk where the bridge
    # imports it from.
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)

    # session_key 12345_67 → chat_id=12345, thread_id=67
    response = bridge.run_claude("hello", "12345_67")

    # build_options received the bot, parsed chat/thread ids.
    assert len(build_options_calls) == 1
    call = build_options_calls[0]
    assert call["bot"] is fake_bot
    assert call["chat_id"] == 12345
    assert call["thread_id"] == 67
    assert call["main_loop"] is fake_loop

    # ClaudeSDKClient was constructed with the v2 options.
    assert len(construct_calls) == 1
    assert construct_calls[0] is sentinel_options

    # MOP instance pinned to session state for the lifetime of the SDK
    # client so its Stop-hook closure stays alive. Cleared after the turn.
    assert mop_during_session == [sentinel_mop]
    state = bridge._sessions.get("12345_67")
    assert state is not None
    assert state.mop is None  # cleared in finally

    # MOP delivers via Telegram itself; run_claude returns "" so the
    # orchestrator's _send_response is a no-op (empty-string short-circuit).
    assert response == ""
