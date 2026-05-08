"""Tests for patchbay.harness.pi.PiHarness.

Uses fake-pi binaries (Python scripts) so we exercise the full
Popen → drain → parse → emit pipeline without touching a real
provider or burning credits.
"""

from __future__ import annotations

import asyncio
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from patchbay.harness import (
    Harness,
    PiHarness,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)
from patchbay.harness.pi import (
    _extract_text,
    _extract_total_cost,
    _find_assistant_error,
    _find_session_id,
    _parse_pi_events,
)


# ---- Fake pi factory ----


def _write_fake_pi(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "fake_pi"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, json, os\n"
        "if __name__ == '__main__':\n"
        + textwrap.indent(body, "    ")
        + "\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _make_req(tmp_path: Path, *, prompt="say hi", **kw) -> TurnRequest:
    defaults: dict = dict(
        prompt=prompt,
        session_key="t",
        project_dir=tmp_path,
        system_prompt="",
        resume_session_id=None,
        model=None,
        effort=None,
        allowed_tools=None,
        disallowed_tools=None,
        max_turns=None,
        plugin_dir=None,
    )
    defaults.update(kw)
    return TurnRequest(**defaults)


async def _drain(gen) -> list[TurnEvent]:
    out: list[TurnEvent] = []
    async for ev in gen:
        out.append(ev)
    return out


# ---- Protocol / capability ----


def test_pi_harness_is_protocol_conformant():
    h: Harness = PiHarness()
    assert h.name == "pi"
    assert h.capabilities.supports_resume is True
    assert h.capabilities.supports_tool_streaming is True


# ---- Pure helpers ----


def test_parse_pi_events_skips_garbage():
    stdout = (
        '{"type":"session","id":"abc"}\n'
        "not json\n"
        "\n"
        '{"type":"agent_end"}\n'
        '"a string, not a dict"\n'
    )
    out = list(_parse_pi_events(stdout))
    assert [e["type"] for e in out] == ["session", "agent_end"]


def test_find_session_id():
    events = [
        {"type": "session", "id": "session-uuid-123"},
        {"type": "agent_start"},
    ]
    assert _find_session_id(events) == "session-uuid-123"


def test_find_session_id_returns_none_when_absent():
    assert _find_session_id([{"type": "agent_start"}]) is None


def test_extract_text_aggregates_deltas_and_prefers_text_end():
    events = [
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": "Hello",
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": " world",
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_end",
                "contentIndex": 0,
                "content": "Hello world!",
            },
        },
    ]
    assert _extract_text(events) == "Hello world!"


def test_extract_text_falls_back_to_deltas_when_no_text_end():
    events = [
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": "partial",
            },
        }
    ]
    assert _extract_text(events) == "partial"


def test_extract_total_cost_sums_message_costs():
    events = [
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "usage": {"cost": {"total": 0.0012}},
            },
        },
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "usage": {"cost": {"total": 0.0034}},
            },
        },
    ]
    assert _extract_total_cost(events) == pytest.approx(0.0046)


def test_extract_total_cost_returns_none_when_no_usage():
    assert _extract_total_cost([{"type": "agent_end"}]) is None


def test_find_assistant_error_picks_up_stop_reason():
    events = [
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "401 bad key",
            },
        }
    ]
    assert _find_assistant_error(events) == "401 bad key"


def test_find_assistant_error_skips_user_messages():
    events = [
        {
            "type": "message_end",
            "message": {
                "role": "user",
                "stopReason": "error",
                "errorMessage": "shouldn't surface",
            },
        }
    ]
    assert _find_assistant_error(events) is None


# ---- Cmd construction ----


def test_build_cmd_includes_required_flags(tmp_path):
    h = PiHarness(pi_path="/fake/pi")
    cmd = h._build_cmd(_make_req(tmp_path, prompt="hi"))
    assert cmd[0] == "/fake/pi"
    assert "-p" in cmd
    assert "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "json"
    # Prompt is the trailing positional.
    assert cmd[-1] == "hi"


def test_build_cmd_passes_session_when_resuming(tmp_path):
    h = PiHarness(pi_path="/fake/pi")
    cmd = h._build_cmd(_make_req(tmp_path, resume_session_id="abc-123"))
    assert "--session" in cmd and cmd[cmd.index("--session") + 1] == "abc-123"


def test_build_cmd_passes_model(tmp_path):
    h = PiHarness(pi_path="/fake/pi")
    cmd = h._build_cmd(_make_req(tmp_path, model="openrouter/deepseek/deepseek-chat"))
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "openrouter/deepseek/deepseek-chat"


def test_build_cmd_passes_system_prompt(tmp_path):
    h = PiHarness(pi_path="/fake/pi")
    cmd = h._build_cmd(_make_req(tmp_path, system_prompt="Be brief."))
    assert "--append-system-prompt" in cmd
    assert cmd[cmd.index("--append-system-prompt") + 1] == "Be brief."


def test_build_cmd_handles_allowed_tools(tmp_path):
    h = PiHarness(pi_path="/fake/pi")
    cmd = h._build_cmd(_make_req(tmp_path, allowed_tools=["read", "grep"]))
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == "read,grep"


def test_build_cmd_extra_flags(tmp_path):
    h = PiHarness(pi_path="/fake/pi")
    cmd = h._build_cmd(
        _make_req(
            tmp_path,
            extra={
                "thinking": "high",
                "no_extensions": True,
                "no_skills": True,
                "no_context_files": True,
            },
        )
    )
    assert "--thinking" in cmd and cmd[cmd.index("--thinking") + 1] == "high"
    assert "--no-extensions" in cmd
    assert "--no-skills" in cmd
    assert "--no-context-files" in cmd


# ---- End-to-end against fake binaries ----


def test_run_turn_happy_path(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        textwrap.dedent(
            """
            for ev in [
                {"type":"session","id":"s-1"},
                {"type":"agent_start"},
                {"type":"turn_start"},
                {"type":"message_start","message":{"role":"assistant","stopReason":"stop","usage":{"cost":{"total":0.0001}}}},
                {"type":"message_update","assistantMessageEvent":{
                    "type":"text_end","contentIndex":0,"content":"Hello!"}},
                {"type":"message_end","message":{"role":"assistant","stopReason":"stop","usage":{"cost":{"total":0.0001}}}},
                {"type":"turn_end","message":{"role":"assistant","stopReason":"stop","usage":{"cost":{"total":0.0001}}},"toolResults":[]},
                {"type":"agent_end"},
            ]:
                print(json.dumps(ev), flush=True)
            """
        ),
    )
    h = PiHarness(pi_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnFinal)
    final = events[-1]
    assert final.session_id == "s-1"
    assert final.raw_text == "Hello!"
    assert final.num_turns == 1
    assert final.total_cost_usd == pytest.approx(0.0001)
    assert any(isinstance(e, TextDelta) and e.text == "Hello!" for e in events)


def test_run_turn_emits_tool_use_and_result(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        textwrap.dedent(
            """
            for ev in [
                {"type":"session","id":"s-tools"},
                {"type":"message_update","assistantMessageEvent":{
                    "type":"toolcall_end","toolCallId":"call_1",
                    "name":"bash","input":{"command":"echo hi"}}},
                {"type":"tool_execution_end","toolCallId":"call_1","output":"hi\\n","isError":False},
                {"type":"message_update","assistantMessageEvent":{
                    "type":"text_end","contentIndex":0,"content":"Done."}},
                {"type":"turn_end","message":{"role":"assistant","stopReason":"stop"}},
                {"type":"agent_end"},
            ]:
                print(json.dumps(ev), flush=True)
            """
        ),
    )
    h = PiHarness(pi_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    tool_uses = [e for e in events if isinstance(e, ToolUse)]
    tool_results = [e for e in events if isinstance(e, ToolResult)]
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "bash"
    assert tool_uses[0].input == {"command": "echo hi"}
    assert tool_uses[0].id == "call_1"
    assert len(tool_results) == 1
    assert tool_results[0].tool_use_id == "call_1"
    assert "hi" in tool_results[0].output


def test_run_turn_classifies_rate_limit(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        textwrap.dedent(
            """
            for ev in [
                {"type":"session","id":"s-rl"},
                {"type":"turn_end","message":{
                    "role":"assistant","stopReason":"error",
                    "errorMessage":"429 rate limit exceeded for tier"}},
                {"type":"agent_end"},
            ]:
                print(json.dumps(ev), flush=True)
            """
        ),
    )
    h = PiHarness(pi_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "rate_limit"


def test_run_turn_classifies_unknown_error(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        textwrap.dedent(
            """
            for ev in [
                {"type":"session","id":"s-err"},
                {"type":"turn_end","message":{
                    "role":"assistant","stopReason":"error",
                    "errorMessage":"401 Incorrect API key"}},
                {"type":"agent_end"},
            ]:
                print(json.dumps(ev), flush=True)
            """
        ),
    )
    h = PiHarness(pi_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "unknown"
    assert "401" in events[-1].message


def test_run_turn_handles_no_output_with_oom_exit(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        "import sys; sys.exit(137)",
    )
    h = PiHarness(pi_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "oom"


def test_run_turn_handles_no_output_with_stale_session(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        textwrap.dedent(
            """
            sys.stderr.write("Session not found: abc-123\\n")
            sys.exit(1)
            """
        ),
    )
    h = PiHarness(pi_path=str(fake))
    events = asyncio.run(
        _drain(h.run_turn(_make_req(tmp_path, resume_session_id="abc-123")))
    )
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "corrupt_session"
    assert events[-1].retryable is True


def test_run_turn_handles_no_output_generic(tmp_path):
    fake = _write_fake_pi(tmp_path, "import sys; sys.exit(0)")
    h = PiHarness(pi_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "unknown"


def test_run_turn_timeout(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        "import time; time.sleep(5)",
    )
    h = PiHarness(pi_path=str(fake), max_timeout_seconds=0.5)
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "timeout"


def test_run_turn_calls_progress_per_stdout_line(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        textwrap.dedent(
            """
            for ev in [
                {"type":"session","id":"s-prog"},
                {"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"a"}},
                {"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"b"}},
                {"type":"message_update","assistantMessageEvent":{"type":"text_end","contentIndex":0,"content":"ab"}},
                {"type":"turn_end","message":{"role":"assistant","stopReason":"stop"}},
                {"type":"agent_end"},
            ]:
                print(json.dumps(ev), flush=True)
            """
        ),
    )
    counter = {"calls": 0}

    def on_progress():
        counter["calls"] += 1

    h = PiHarness(pi_path=str(fake), on_progress=on_progress)
    asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    # At least one call per stdout line we emitted.
    assert counter["calls"] >= 4


def test_run_turn_calls_proc_setter(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        textwrap.dedent(
            """
            print(json.dumps({"type":"session","id":"s"}), flush=True)
            print(json.dumps({"type":"turn_end","message":{"role":"assistant","stopReason":"stop"}}), flush=True)
            print(json.dumps({"type":"agent_end"}), flush=True)
            """
        ),
    )
    seen: list = []
    h = PiHarness(pi_path=str(fake), proc_setter=lambda p: seen.append(p))
    asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    # First call sets the proc, last call clears it.
    assert seen[0] is not None
    assert seen[-1] is None


def test_run_turn_falls_back_when_no_events_parsed(tmp_path):
    fake = _write_fake_pi(
        tmp_path,
        "print('plain text, not json')\n",
    )
    h = PiHarness(pi_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnFinal)
    assert "plain text" in events[-1].raw_text


def test_run_turn_empty_text_emits_placeholder(tmp_path):
    """Successful agent_end with no assistant text should still finish cleanly."""
    fake = _write_fake_pi(
        tmp_path,
        textwrap.dedent(
            """
            for ev in [
                {"type":"session","id":"s-empty"},
                {"type":"turn_end","message":{"role":"assistant","stopReason":"stop"}},
                {"type":"agent_end"},
            ]:
                print(json.dumps(ev), flush=True)
            """
        ),
    )
    h = PiHarness(pi_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnFinal)
    assert "no text response" in events[-1].raw_text


def test_cancel_kills_running_proc(tmp_path):
    fake = _write_fake_pi(tmp_path, "import time; time.sleep(30)")
    h = PiHarness(pi_path=str(fake), max_timeout_seconds=10)

    async def run_and_cancel() -> list[TurnEvent]:
        task = asyncio.create_task(_drain(h.run_turn(_make_req(tmp_path))))
        await asyncio.sleep(0.2)
        await h.cancel()
        return await task

    events = asyncio.run(run_and_cancel())
    # Cancel doesn't manufacture a clean Final; whatever the parser sees
    # after kill becomes the terminator. We just want to know we exited.
    assert events
