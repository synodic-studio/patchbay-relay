"""Tests for stargate.harness.claude_sdk_channel.ClaudeSdkChannel.

These tests stub the SDK client so they don't burn API credits. The
channel is a thin wrapper — its job is to drive the SDK client's
async API and translate messages into TurnEvents on a queue. We
verify each translation rule and the lifecycle (open/push/interrupt/
close) against a fake client.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest

from stargate.harness import (
    ChannelHandle,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnError,
    TurnFinal,
)
from stargate.harness.claude_sdk_channel import (
    ClaudeSdkChannel,
    _stringify_tool_output,
    _terminator_from_result,
)


# ---- Fake SDK ----


class _FakeMessage:
    """Base for the fake SDK message types we inject."""


class _FakeAssistant(_FakeMessage):
    def __init__(self, content, error=None):
        self.content = content
        self.error = error


class _FakeUser(_FakeMessage):
    def __init__(self, content):
        self.content = content


class _FakeSystem(_FakeMessage):
    def __init__(self, data):
        self.data = data


class _FakeResult(_FakeMessage):
    def __init__(
        self,
        *,
        session_id="ses-1",
        num_turns=1,
        total_cost_usd=0.001,
        result="",
        subtype="success",
        is_error=False,
        errors=None,
    ):
        self.session_id = session_id
        self.num_turns = num_turns
        self.total_cost_usd = total_cost_usd
        self.result = result
        self.subtype = subtype
        self.is_error = is_error
        self.errors = errors or []


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeToolUseBlock:
    def __init__(self, name, input, id):
        self.name = name
        self.input = input
        self.id = id


class _FakeToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class _FakeClient:
    """Stand-in for ClaudeSDKClient. Records calls and replays scripted messages."""

    def __init__(self, messages, *, raise_on=None):
        self._scripted: list[Any] = list(messages)
        self._extra_query_responses: list[list[Any]] = []
        self._raise_on = raise_on or {}
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.interrupt_calls = 0
        self.queries: list[str] = []
        self._open = False
        self._yield_event = asyncio.Event()
        self._yield_event.set()

    async def connect(self, prompt=None):
        self.connect_calls += 1
        self._open = True

    async def disconnect(self):
        self.disconnect_calls += 1
        self._open = False
        # Unblock any waiting receive_messages so it can finish.
        self._yield_event.set()

    async def interrupt(self):
        self.interrupt_calls += 1

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)
        # Allow callers to script "after second push, emit these messages."
        if self._extra_query_responses:
            self._scripted.extend(self._extra_query_responses.pop(0))
            self._yield_event.set()

    def queue_response_for_next_query(self, messages):
        self._extra_query_responses.append(list(messages))

    async def receive_messages(self):
        while True:
            while self._scripted:
                msg = self._scripted.pop(0)
                if "before_message" in self._raise_on:
                    exc = self._raise_on["before_message"]
                    self._raise_on = {}
                    raise exc
                yield msg
            if not self._open:
                return
            self._yield_event.clear()
            try:
                await asyncio.wait_for(self._yield_event.wait(), timeout=2)
            except asyncio.TimeoutError:
                return


@pytest.fixture
def fake_sdk(monkeypatch):
    """Inject a fake `claude_agent_sdk` module before the channel imports it."""
    module = types.ModuleType("claude_agent_sdk")
    module.AssistantMessage = _FakeAssistant
    module.UserMessage = _FakeUser
    module.SystemMessage = _FakeSystem
    module.ResultMessage = _FakeResult
    module.TextBlock = _FakeTextBlock
    module.ToolUseBlock = _FakeToolUseBlock
    module.ToolResultBlock = _FakeToolResultBlock

    class _CLIError(Exception):
        pass

    class _ProcessError(Exception):
        def __init__(self, exit_code=1, stderr=""):
            super().__init__(stderr)
            self.exit_code = exit_code
            self.stderr = stderr

    class _CLIJSONDecodeError(Exception):
        def __init__(self, msg="bad", line=""):
            super().__init__(msg)
            self.line = line

    module.CLINotFoundError = _CLIError
    module.CLIConnectionError = _CLIError
    module.ProcessError = _ProcessError
    module.CLIJSONDecodeError = _CLIJSONDecodeError

    # The channel constructs a client itself in open(); override the class
    # so the channel ends up holding our fake.
    holder: dict = {"client": None}

    class _ClientFactory:
        def __init__(self, options=None):
            holder["options"] = options
            holder["client"] = _FakeClient(messages=holder.get("script", []))

        def __new__(cls, options=None):  # type: ignore[no-untyped-def]
            client = holder.get("client")
            if client is None:
                client = _FakeClient(messages=holder.get("script", []))
                holder["client"] = client
            holder["options"] = options
            return client

    module.ClaudeSDKClient = _ClientFactory
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return holder


# ---- Pure helpers ----


def test_stringify_tool_output_handles_str_list_none():
    assert _stringify_tool_output(None) == ""
    assert _stringify_tool_output("hello") == "hello"
    assert _stringify_tool_output([{"text": "a"}, {"text": "b"}]) == "a\nb"
    assert _stringify_tool_output(["raw", 42]) == "raw\n42"


def test_terminator_from_result_max_turns():
    msg = _FakeResult(subtype="max_turns", num_turns=10)
    ev = _terminator_from_result(msg, "")
    assert isinstance(ev, TurnError)
    assert ev.kind == "max_turns"


def test_terminator_from_result_rate_limit_via_errors_list():
    msg = _FakeResult(is_error=True, errors=["429 too many requests"])
    ev = _terminator_from_result(msg, "")
    assert isinstance(ev, TurnError)
    assert ev.kind == "rate_limit"


def test_terminator_from_result_unknown_error():
    msg = _FakeResult(is_error=True, errors=["something blew up"])
    ev = _terminator_from_result(msg, "")
    assert isinstance(ev, TurnError)
    assert ev.kind == "unknown"


def test_terminator_from_result_success():
    msg = _FakeResult(session_id="ses-2", num_turns=3, total_cost_usd=0.5)
    ev = _terminator_from_result(msg, "Hi.")
    assert isinstance(ev, TurnFinal)
    assert ev.session_id == "ses-2"
    assert ev.raw_text == "Hi."


# ---- Channel lifecycle ----


def test_channel_implements_handle_protocol():
    chan = ClaudeSdkChannel(options=None)
    assert isinstance(chan, ChannelHandle)


def test_open_calls_connect_and_initial_query(fake_sdk):
    fake_sdk["script"] = [_FakeResult(session_id="ses-init")]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("hi")
        # Drain the one final the script gave us
        events = []
        async for ev in chan.events():
            events.append(ev)
            if isinstance(ev, TurnFinal):
                await chan.close()
        return chan, events

    chan, events = asyncio.run(go())
    assert fake_sdk["client"].connect_calls == 1
    assert fake_sdk["client"].queries == ["hi"]
    assert chan.session_id == "ses-init"
    assert any(isinstance(e, TurnFinal) for e in events)


def test_push_calls_query(fake_sdk):
    fake_sdk["script"] = [_FakeResult(session_id="ses-x")]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("first")
        # Wait for first turn to drain
        async for ev in chan.events():
            if isinstance(ev, TurnFinal):
                break
        # Queue more events for second turn before push
        fake_sdk["client"].queue_response_for_next_query(
            [_FakeAssistant([_FakeTextBlock("second")]), _FakeResult(session_id="ses-x")]
        )
        await chan.push("second prompt")
        finals = 0
        async for ev in chan.events():
            if isinstance(ev, TurnFinal):
                finals += 1
                if finals >= 1:
                    break
        await chan.close()
        return finals

    finals = asyncio.run(go())
    assert finals >= 1
    assert fake_sdk["client"].queries == ["first", "second prompt"]


def test_push_after_close_raises(fake_sdk):
    fake_sdk["script"] = [_FakeResult()]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("hi")
        async for ev in chan.events():
            if isinstance(ev, TurnFinal):
                break
        await chan.close()
        with pytest.raises(RuntimeError):
            await chan.push("nope")

    asyncio.run(go())


def test_push_before_open_raises(fake_sdk):
    chan = ClaudeSdkChannel(options=None)

    async def go():
        with pytest.raises(RuntimeError):
            await chan.push("nope")

    asyncio.run(go())


def test_interrupt_forwards_to_client(fake_sdk):
    fake_sdk["script"] = [_FakeResult()]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("x")
        await chan.interrupt()
        async for _ in chan.events():
            pass
        await chan.close()

    asyncio.run(go())
    assert fake_sdk["client"].interrupt_calls == 1


def test_close_is_idempotent(fake_sdk):
    fake_sdk["script"] = [_FakeResult()]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("x")
        async for _ in chan.events():
            pass
        await chan.close()
        await chan.close()  # second call must not raise
        assert fake_sdk["client"].disconnect_calls == 1

    asyncio.run(go())


def test_assistant_text_blocks_become_text_deltas(fake_sdk):
    fake_sdk["script"] = [
        _FakeAssistant([_FakeTextBlock("Hello "), _FakeTextBlock("world")]),
        _FakeResult(session_id="s"),
    ]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("x")
        events = []
        async for ev in chan.events():
            events.append(ev)
            if isinstance(ev, TurnFinal):
                break
        await chan.close()
        return events

    events = asyncio.run(go())
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert [d.text for d in deltas] == ["Hello ", "world"]
    final = next(e for e in events if isinstance(e, TurnFinal))
    assert final.raw_text == "Hello world"


def test_tool_use_block_becomes_tool_use_event(fake_sdk):
    fake_sdk["script"] = [
        _FakeAssistant([_FakeToolUseBlock("bash", {"command": "ls"}, "id-1")]),
        _FakeUser([_FakeToolResultBlock("id-1", "out", is_error=False)]),
        _FakeResult(session_id="s"),
    ]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("x")
        out = []
        async for ev in chan.events():
            out.append(ev)
            if isinstance(ev, TurnFinal):
                break
        await chan.close()
        return out

    events = asyncio.run(go())
    uses = [e for e in events if isinstance(e, ToolUse)]
    results = [e for e in events if isinstance(e, ToolResult)]
    assert len(uses) == 1 and uses[0].name == "bash"
    assert len(results) == 1 and results[0].output == "out"


def test_assistant_rate_limit_error_emits_turn_error(fake_sdk):
    fake_sdk["script"] = [
        _FakeAssistant([], error="rate_limit"),
        _FakeResult(session_id="s"),  # still arrives but channel kept going
    ]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("x")
        out = []
        async for ev in chan.events():
            out.append(ev)
            if isinstance(ev, TurnFinal) or len(out) > 5:
                break
        await chan.close()
        return out

    events = asyncio.run(go())
    errors = [e for e in events if isinstance(e, TurnError)]
    assert any(e.kind == "rate_limit" for e in errors)


def test_system_message_session_id_captured(fake_sdk):
    fake_sdk["script"] = [
        _FakeSystem(data={"session_id": "ses-from-system"}),
        _FakeResult(session_id="ses-from-result"),
    ]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("x")
        async for ev in chan.events():
            if isinstance(ev, TurnFinal):
                break
        sid = chan.session_id
        await chan.close()
        return sid

    sid = asyncio.run(go())
    # ResultMessage's session_id wins (more authoritative).
    assert sid == "ses-from-result"


def test_drain_unexpected_exception_surfaces_as_turn_error(fake_sdk):
    fake_sdk["script"] = [_FakeResult()]

    async def go():
        chan = ClaudeSdkChannel(options=None)
        await chan.open("x")
        # Inject an exception path by forcing the fake client to raise next.
        fake_sdk["client"]._raise_on = {"before_message": ValueError("boom")}
        # New round of messages so the drain wakes up
        fake_sdk["client"].queue_response_for_next_query([_FakeResult()])
        await chan.push("trigger")
        out = []
        async for ev in chan.events():
            out.append(ev)
            if isinstance(ev, TurnError):
                break
            if len(out) > 20:
                break
        await chan.close()
        return out

    events = asyncio.run(go())
    assert any(isinstance(e, TurnError) and e.kind == "unknown" for e in events)
