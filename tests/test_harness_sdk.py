"""ClaudeSdkHarness tests.

The SDK still drives the `claude` CLI as a subprocess, so a real
end-to-end test would burn API credits on every run. Instead we stub
`ClaudeSDKClient` at the module level — an async context manager that
yields a scripted sequence of SDK messages — and assert the harness
emits the right TurnEvent stream.

Same contract as test_harness_contract.py: stream ends with exactly one
TurnFinal *or* TurnError.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from patchbay.harness import (
    ClaudeSdkHarness,
    Harness,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)


# ---------------------------------------------------------------------------
# Stub SDK client — async context manager that yields scripted messages.
# ---------------------------------------------------------------------------


class _StubClient:
    """Stand-in for ClaudeSDKClient.

    Constructed with a list of messages to yield (or an exception to raise
    instead). Honors the async context manager + `query()` +
    `receive_messages()` shape the harness consumes. `receive_response`
    is kept as a thin alias because the older harness used it and a few
    tests still poke that name directly.
    """

    def __init__(self, *, messages: list = None, raise_in_stream: Exception = None):
        self._messages = messages or []
        self._raise = raise_in_stream
        self.queries: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_messages(self) -> AsyncIterator:
        if self._raise is not None:
            raise self._raise
        for msg in self._messages:
            yield msg

    # Back-compat alias — the harness moved from receive_response to
    # receive_messages when inflight push was wired (the message loop
    # needs to keep draining past the first ResultMessage when more
    # queries are in flight). Keep this so older tests still work.
    receive_response = receive_messages


def _make_request(*, tmp_path: Path, prompt: str = "hi", resume: str | None = None) -> TurnRequest:
    return TurnRequest(
        prompt=prompt,
        session_key="test_42",
        project_dir=tmp_path,
        system_prompt="ignore",
        resume_session_id=resume,
        model=None,
        effort=None,
        allowed_tools=None,
        disallowed_tools=["AskUserQuestion"],
        max_turns=5,
        plugin_dir=None,
    )


def _run(harness: Harness, req: TurnRequest, stub_factory) -> list[TurnEvent]:
    """Patch ClaudeSDKClient with `stub_factory` and collect the stream."""
    async def _go() -> list[TurnEvent]:
        # claude_sdk.py imports ClaudeSDKClient *inside* run_turn (lazy), so
        # patching the symbol on the source module catches the import at call time.
        with patch("claude_agent_sdk.ClaudeSDKClient", stub_factory):
            events: list[TurnEvent] = []
            async for ev in harness.run_turn(req):
                events.append(ev)
            return events
    return asyncio.run(_go())


def _has_exactly_one_terminator(events: list[TurnEvent]) -> bool:
    if not events:
        return False
    terms = [e for e in events if isinstance(e, (TurnFinal, TurnError))]
    return len(terms) == 1 and isinstance(events[-1], (TurnFinal, TurnError))


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_implements_harness_protocol(self):
        assert isinstance(ClaudeSdkHarness(), Harness)

    def test_capabilities(self):
        caps = ClaudeSdkHarness().capabilities
        assert caps.supports_resume is True
        assert caps.supports_tool_streaming is True   # diff from CLI harness
        assert caps.supports_interrupt is True        # diff from CLI harness
        assert caps.supports_effort is True
        assert caps.supports_mcp is True

    def test_name_is_cc_sdk(self):
        assert ClaudeSdkHarness().name == "cc-sdk"


# ---------------------------------------------------------------------------
# Message → event mapping
# ---------------------------------------------------------------------------


def _result_msg(**kw) -> ResultMessage:
    """Build a ResultMessage with sane defaults; override via kwargs."""
    defaults = dict(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        session_id="sdk-sess-1",
        stop_reason="end_turn",
        total_cost_usd=0.001,
        usage=None,
        result=None,
        structured_output=None,
        model_usage=None,
        permission_denials=None,
        errors=None,
        uuid=None,
    )
    defaults.update(kw)
    return ResultMessage(**defaults)


def _assistant_msg(*, content, error=None, model="claude-opus-4-7") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        model=model,
        parent_tool_use_id=None,
        error=error,
        usage=None,
        message_id=None,
        stop_reason=None,
        session_id="sdk-sess-1",
        uuid=None,
    )


class TestSuccessfulTurn:
    def test_text_block_yields_textdelta(self, tmp_path):
        msgs = [
            _assistant_msg(content=[TextBlock(text="hello world")]),
            _result_msg(result="hello world"),
        ]
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(messages=msgs),
        )
        assert _has_exactly_one_terminator(events)
        assert isinstance(events[-1], TurnFinal)
        assert events[-1].session_id == "sdk-sess-1"
        assert events[-1].num_turns == 1
        deltas = [e for e in events if isinstance(e, TextDelta)]
        assert len(deltas) == 1
        assert deltas[0].text == "hello world"
        assert "hello world" in events[-1].raw_text

    def test_tool_use_block_yields_tooluse_event(self, tmp_path):
        msgs = [
            _assistant_msg(content=[
                ToolUseBlock(id="tu_1", name="Read", input={"file_path": "/x"})
            ]),
            _assistant_msg(content=[TextBlock(text="ok")]),
            _result_msg(result="ok"),
        ]
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(messages=msgs),
        )
        tool_uses = [e for e in events if isinstance(e, ToolUse)]
        assert len(tool_uses) == 1
        assert tool_uses[0].name == "Read"
        assert tool_uses[0].input == {"file_path": "/x"}
        assert tool_uses[0].id == "tu_1"
        assert _has_exactly_one_terminator(events)

    def test_tool_result_via_user_message(self, tmp_path):
        msgs = [
            UserMessage(
                content=[ToolResultBlock(tool_use_id="tu_1", content="file body", is_error=False)],
                uuid=None,
                parent_tool_use_id=None,
                tool_use_result=None,
            ),
            _assistant_msg(content=[TextBlock(text="done")]),
            _result_msg(result="done"),
        ]
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(messages=msgs),
        )
        results = [e for e in events if isinstance(e, ToolResult)]
        assert len(results) == 1
        assert results[0].tool_use_id == "tu_1"
        assert "file body" in results[0].output
        assert results[0].is_error is False

    def test_system_message_init_does_not_break_stream(self, tmp_path):
        msgs = [
            SystemMessage(subtype="init", data={"session_id": "from-system"}),
            _assistant_msg(content=[TextBlock(text="hi")]),
            _result_msg(result="hi"),
        ]
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(messages=msgs),
        )
        assert _has_exactly_one_terminator(events)
        assert isinstance(events[-1], TurnFinal)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_assistant_rate_limit_error_typed(self, tmp_path):
        """AssistantMessage.error == 'rate_limit' is the SDK's typed signal —
        cleaner than string-matching stderr."""
        msgs = [_assistant_msg(content=[TextBlock(text="")], error="rate_limit")]
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(messages=msgs),
        )
        assert _has_exactly_one_terminator(events)
        assert isinstance(events[-1], TurnError)
        assert events[-1].kind == "rate_limit"
        assert events[-1].retryable is False

    def test_max_turns_subtype(self, tmp_path):
        msgs = [
            _assistant_msg(content=[TextBlock(text="partial")]),
            _result_msg(subtype="max_turns", num_turns=5, result="partial"),
        ]
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(messages=msgs),
        )
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "max_turns"
        assert term.metadata["num_turns"] == 5

    def test_result_is_error_yields_unknown(self, tmp_path):
        msgs = [_result_msg(is_error=True, errors=["something broke"], subtype="error")]
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(messages=msgs),
        )
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "unknown"
        assert "something broke" in term.message

    def test_process_error_137_yields_oom(self, tmp_path):
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(raise_in_stream=ProcessError("killed", exit_code=137, stderr="killed\n")),
        )
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "oom"
        assert term.retryable is True
        assert term.metadata["exit_code"] == 137

    def test_process_error_with_quota_stderr_yields_rate_limit(self, tmp_path):
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(
                raise_in_stream=ProcessError(
                    "quota",
                    exit_code=1,
                    stderr="Please wait and try again later",
                )
            ),
        )
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "rate_limit"

    def test_process_error_with_stale_session_yields_corrupt_session(self, tmp_path):
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path, resume="stale-id"),
            lambda options: _StubClient(
                raise_in_stream=ProcessError(
                    "stale",
                    exit_code=1,
                    stderr="No conversation found with session ID stale-id",
                )
            ),
        )
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "corrupt_session"

    def test_cli_json_decode_error_yields_corrupt_session(self, tmp_path):
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(
                raise_in_stream=CLIJSONDecodeError("bad json", original_error=ValueError("x"))
            ),
        )
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "corrupt_session"

    def test_cli_not_found_yields_process_died_non_retryable(self, tmp_path):
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(raise_in_stream=CLINotFoundError("not installed")),
        )
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "process_died"
        assert term.retryable is False

    def test_cli_connection_error_yields_process_died_retryable(self, tmp_path):
        events = _run(
            ClaudeSdkHarness(),
            _make_request(tmp_path=tmp_path),
            lambda options: _StubClient(raise_in_stream=CLIConnectionError("conn lost")),
        )
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "process_died"
        assert term.retryable is True


class TestTimeout:
    def test_timeout_yields_turnerror_timeout(self, tmp_path):
        # Build a stub that yields nothing forever, then race it against a
        # tiny harness timeout.
        class _HangingClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *e): return None
            async def query(self, p): return None
            async def receive_messages(self):
                if False:  # never True; just makes this an async generator
                    yield None
                await asyncio.sleep(10)
            receive_response = receive_messages

        harness = ClaudeSdkHarness(max_timeout_seconds=0.2)
        events = _run(
            harness,
            _make_request(tmp_path=tmp_path),
            lambda options: _HangingClient(),
        )
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "timeout"


# ---------------------------------------------------------------------------
# Options building
# ---------------------------------------------------------------------------


class TestOptionsBuilding:
    def _build(self, req: TurnRequest):
        from claude_agent_sdk import ClaudeAgentOptions
        return ClaudeSdkHarness()._build_options(req, ClaudeAgentOptions)

    def test_resume_propagates(self, tmp_path):
        opts = self._build(_make_request(tmp_path=tmp_path, resume="abc-123"))
        assert opts.resume == "abc-123"

    def test_resume_omitted_when_absent(self, tmp_path):
        opts = self._build(_make_request(tmp_path=tmp_path, resume=None))
        assert opts.resume is None

    def test_permission_mode_is_bypass(self, tmp_path):
        opts = self._build(_make_request(tmp_path=tmp_path))
        assert opts.permission_mode == "bypassPermissions"

    def test_disallowed_tools_propagates(self, tmp_path):
        opts = self._build(_make_request(tmp_path=tmp_path))
        assert "AskUserQuestion" in opts.disallowed_tools

    def test_plugin_dir_routed_via_extra_args(self, tmp_path):
        req = TurnRequest(
            prompt="x",
            session_key="k",
            project_dir=tmp_path,
            system_prompt="",
            resume_session_id=None,
            model=None,
            effort=None,
            allowed_tools=None,
            disallowed_tools=None,
            max_turns=10,
            plugin_dir="/some/plugin",
        )
        opts = self._build(req)
        assert opts.extra_args.get("plugin-dir") == "/some/plugin"

    def test_system_prompt_uses_preset_append_form(self, tmp_path):
        req = _make_request(tmp_path=tmp_path)
        opts = self._build(req)
        # Dict form with append=… so the SDK extends the claude_code preset
        # instead of replacing it.
        assert isinstance(opts.system_prompt, dict)
        assert opts.system_prompt.get("append") == "ignore"
        assert opts.system_prompt.get("preset") == "claude_code"

    def test_cwd_set_to_project_dir(self, tmp_path):
        opts = self._build(_make_request(tmp_path=tmp_path))
        assert opts.cwd == str(tmp_path)


# ---------------------------------------------------------------------------
# Cancellation — `harness.cancel()` must actually interrupt the running turn
# ---------------------------------------------------------------------------


class TestCancellation:
    """Regression: `self._task` was declared but never assigned, so
    `cancel()` was a silent no-op. The bridge's stall detector / `/kill`
    appeared to cancel cc-sdk turns (logging + user notification fired)
    while the SDK actually kept running. The user observed a "killed" message
    followed by a successful response moments later.
    """

    def test_task_handle_set_during_run_and_cleared_after(self, tmp_path):
        harness = ClaudeSdkHarness()
        # Stub yields one ResultMessage so run_turn completes quickly.
        client = _StubClient(messages=[_result_msg()])

        events = _run(harness, _make_request(tmp_path=tmp_path), lambda options: client)
        # Post-run, the task handle must be cleared so the next turn doesn't
        # inherit a stale (already-done) task and silently no-op cancel().
        assert harness._task is None
        # And the turn actually produced a terminator.
        assert _has_exactly_one_terminator(events)

    def test_cancel_actually_stops_inflight_turn(self, tmp_path):
        """The real fix: cancel() must interrupt the running task. Use a
        client that yields nothing and never returns to simulate a long
        tool call; cancel after a short delay and assert the turn exits."""
        harness = ClaudeSdkHarness()

        class _NeverEndingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return None

            async def query(self, prompt: str) -> None:
                pass

            async def receive_messages(self):
                # Block forever — simulates a tool call with no events.
                await asyncio.Event().wait()
                yield  # pragma: no cover — sentinel for type checker

            receive_response = receive_messages

        async def _go() -> str:
            with patch("claude_agent_sdk.ClaudeSDKClient", lambda options: _NeverEndingClient()):
                gen = harness.run_turn(_make_request(tmp_path=tmp_path))

                async def _drive():
                    async for _ in gen:
                        pass
                    return "completed"

                drive_task = asyncio.create_task(_drive())
                # Let run_turn enter the receive loop and assign self._task.
                for _ in range(50):
                    if harness._task is not None:
                        break
                    await asyncio.sleep(0.01)
                assert harness._task is not None, "run_turn did not capture its task"
                await harness.cancel()
                # The drive task must finish (cancellation propagates out
                # of the generator), not hang forever.
                try:
                    return await asyncio.wait_for(drive_task, timeout=2.0)
                except asyncio.CancelledError:
                    return "cancelled"

        result = asyncio.run(_go())
        assert result in ("cancelled", "completed")
        # And the handle is cleared so a follow-up cancel is a no-op, not a
        # double-fire.
        assert harness._task is None

    def test_cancel_is_noop_when_no_active_turn(self, tmp_path):
        """Cancel before any turn started must not raise."""
        harness = ClaudeSdkHarness()
        asyncio.run(harness.cancel())
        assert harness._task is None


# ---------------------------------------------------------------------------
# Inflight push — `push()` must route a new user message into a turn that
# is still mid-stream, and the receive loop must keep draining until the
# pushed query's ResultMessage arrives.
# ---------------------------------------------------------------------------


class TestInflightPush:
    def test_push_returns_false_when_no_live_client(self, tmp_path):
        """Outside of a turn, push() has nothing to inject into. The bridge
        falls back to the queue in that case, so push must NOT raise."""
        harness = ClaudeSdkHarness()
        accepted = asyncio.run(harness.push("hello again"))
        assert accepted is False
        # And it didn't leak inflight-query bookkeeping across calls.
        assert harness._inflight_queries == 0

    def test_push_mid_turn_routes_into_same_client_and_drains_extra_result(self, tmp_path):
        """The full path: while run_turn is between the initial query and
        its ResultMessage, push() injects a second query; the harness
        keeps draining receive_messages() until BOTH ResultMessages have
        been consumed; the final TurnFinal aggregates text from both.

        Without the inflight-push wiring, push() would either error
        ("no live client") or the receive loop would exit on the first
        ResultMessage and the response to the pushed query would be lost.
        """

        # A controllable async iterator so the test can interleave with
        # the receive loop. The test pushes between the first
        # AssistantMessage and the first ResultMessage.
        gate_before_first_result = asyncio.Event()
        push_done = asyncio.Event()

        class _ControllableClient:
            def __init__(self):
                self.queries: list[str] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return None

            async def query(self, prompt: str) -> None:
                self.queries.append(prompt)

            async def receive_messages(self):
                # First query's stream: text then a ResultMessage, but
                # we pause before the ResultMessage so the test can
                # call push() while inflight_queries is still 1.
                yield AssistantMessage(
                    content=[TextBlock(text="A")], model="m", error=None
                )
                gate_before_first_result.set()
                await push_done.wait()
                yield _result_msg(session_id="s1", num_turns=1)
                # Pushed query's stream: text then its own ResultMessage.
                yield AssistantMessage(
                    content=[TextBlock(text="B")], model="m", error=None
                )
                yield _result_msg(session_id="s1", num_turns=2)

            receive_response = receive_messages

        harness = ClaudeSdkHarness()
        stub_factory = lambda options: _ControllableClient()  # noqa: E731

        async def _go() -> list[TurnEvent]:
            collected: list[TurnEvent] = []
            with patch("claude_agent_sdk.ClaudeSDKClient", stub_factory):
                gen = harness.run_turn(_make_request(tmp_path=tmp_path))

                async def _drain():
                    async for ev in gen:
                        collected.append(ev)

                drive = asyncio.create_task(_drain())
                # Wait until the harness is between the first Assistant
                # message and the first ResultMessage, then push.
                await asyncio.wait_for(gate_before_first_result.wait(), timeout=2.0)
                accepted = await harness.push("second user message")
                assert accepted is True
                push_done.set()
                await asyncio.wait_for(drive, timeout=2.0)
            return collected

        events = asyncio.run(_go())
        # Exactly one terminator, at the end, and it's a TurnFinal — the
        # receive loop did NOT exit on the first ResultMessage.
        assert _has_exactly_one_terminator(events)
        assert isinstance(events[-1], TurnFinal)
        # Both AssistantMessage text blocks made it into the final text.
        assert "A" in events[-1].raw_text and "B" in events[-1].raw_text
        # And bookkeeping cleared after the turn.
        assert harness._inflight_queries == 0
        assert harness._live_client is None

    def test_push_after_error_terminator_does_not_hang(self, tmp_path):
        """If the SDK raises mid-stream, run_turn yields a TurnError and
        returns. _live_client is cleared in the finally so a subsequent
        push() bounces (returns False) instead of hanging the bridge."""
        harness = ClaudeSdkHarness()
        # Trigger a corrupt_session terminator and confirm the harness
        # tears down _live_client cleanly.
        client = _StubClient(raise_in_stream=CLIJSONDecodeError("bad", ValueError("x")))
        events = _run(harness, _make_request(tmp_path=tmp_path), lambda options: client)
        assert isinstance(events[-1], TurnError)
        assert harness._live_client is None
        # Subsequent push has nothing to inject.
        assert asyncio.run(harness.push("late")) is False
