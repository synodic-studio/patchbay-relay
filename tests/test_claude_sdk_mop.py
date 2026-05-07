"""Tests for ClaudeSdkMopHarness — v2 in-process MCP + Stop hook.

The legacy buffer-then-evaluate run_turn path was removed in T15. These
tests cover the v2 surface: build_options() returns a ClaudeAgentOptions
wired with an in-process MOP MCP server and Stop hook, the harness is
registered with the right capabilities, and bridge.run_claude routes
cc-sdk-mop through that path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


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
    options, mop_instance = h.build_options(
        bot=MagicMock(),
        chat_id=42,
        thread_id=None,
        main_loop=asyncio.get_event_loop(),
        rules_dir=None,
    )
    assert isinstance(mop_instance, MOP)
    assert "mop" in options.mcp_servers
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
    # v2 doesn't wrap an inner harness, so context/compact aren't supported.
    assert caps.supports_context_query is False
    assert caps.supports_compact is False


# ---------------------------------------------------------------------------
# bridge.run_claude routes cc-sdk-mop through v2 build_options
# ---------------------------------------------------------------------------

def test_run_claude_cc_sdk_mop_v2_uses_build_options(monkeypatch, tmp_path):
    """run_claude with effective_harness=cc-sdk-mop calls harness.build_options
    and instantiates ClaudeSDKClient with the returned options. The MOP
    instance is held on SessionState.mop so its Stop hook closure stays alive
    for the lifetime of the SDK client.
    """
    import bridge

    monkeypatch.setattr(bridge, "get_chat_harness", lambda key: "cc-sdk-mop")
    monkeypatch.setattr(bridge, "get_chat_working_dir", lambda key: str(tmp_path))
    monkeypatch.setattr(bridge, "get_chat_agent", lambda key: None)
    monkeypatch.setattr(bridge, "get_session_id", lambda key: None)
    monkeypatch.setattr(bridge, "save_session_id", lambda key, sid: None)
    monkeypatch.setattr(bridge, "_load_chat_projects", lambda: {})

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()
    monkeypatch.setattr(bridge, "_bot_instance", fake_bot)

    fake_loop = MagicMock(name="main_loop")
    monkeypatch.setattr(bridge, "_main_loop", fake_loop)

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
            st = bridge._sessions.get("12345_67")
            mop_during_session.append(st.mop if st else None)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def query(self, prompt):
            self.last_prompt = prompt

        async def receive_response(self):
            yield FakeResultMessage()

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)

    response = bridge.run_claude("hello", "12345_67")

    assert len(build_options_calls) == 1
    call = build_options_calls[0]
    assert call["bot"] is fake_bot
    assert call["chat_id"] == 12345
    assert call["thread_id"] == 67
    assert call["main_loop"] is fake_loop

    assert len(construct_calls) == 1
    assert construct_calls[0] is sentinel_options

    assert mop_during_session == [sentinel_mop]
    state = bridge._sessions.get("12345_67")
    assert state is not None
    assert state.mop is None  # cleared in finally

    assert response == ""
