"""Tests for patchbay.harness.opencode.OpenCodeHarness."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from patchbay.harness import (
    Harness,
    OpenCodeHarness,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)
from patchbay.harness.opencode import (
    DEFAULT_OPENCODE_MODEL,
    _find_session_id,
    _parse_opencode_events,
)


def _write_fake(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "fake_opencode"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, json, os\n"
        "if __name__ == '__main__':\n"
        + textwrap.indent(body, "    ")
        + "\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _make_req(tmp_path: Path, *, prompt="hi", **kw) -> TurnRequest:
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


# ---- Pure helpers ----


def test_parse_opencode_events_skips_non_json_lines():
    stdout = (
        "Some banner\n"
        '{"type":"step_start","sessionID":"ses_1"}\n'
        "another banner\n"
        '{"type":"step_finish","sessionID":"ses_1"}\n'
    )
    out = list(_parse_opencode_events(stdout))
    assert [e["type"] for e in out] == ["step_start", "step_finish"]


def test_find_session_id_picks_first_ses():
    events = [
        {"type": "step_start", "sessionID": "ses_abc"},
        {"type": "step_finish", "sessionID": "ses_abc"},
    ]
    assert _find_session_id(events) == "ses_abc"


def test_find_session_id_returns_none_when_absent():
    assert _find_session_id([{"type": "step_start"}]) is None


# ---- Capability / protocol ----


def test_opencode_harness_is_protocol_conformant():
    h: Harness = OpenCodeHarness()
    assert h.name == "opencode"
    assert h.capabilities.supports_resume is True
    assert h.capabilities.supports_tool_streaming is True
    assert h.capabilities.supports_effort is True


# ---- Cmd construction ----


def test_build_cmd_basic_flags(tmp_path):
    h = OpenCodeHarness(opencode_path="/fake/opencode")
    cmd = h._build_cmd(_make_req(tmp_path, prompt="do a thing"))
    assert cmd[0] == "/fake/opencode"
    assert "run" in cmd
    assert "--format" in cmd and cmd[cmd.index("--format") + 1] == "json"
    assert "--pure" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[-1] == "do a thing"


def test_build_cmd_passes_session_when_resuming(tmp_path):
    h = OpenCodeHarness(opencode_path="/fake/opencode")
    cmd = h._build_cmd(_make_req(tmp_path, resume_session_id="ses_abc123"))
    assert "--session" in cmd and cmd[cmd.index("--session") + 1] == "ses_abc123"


def test_build_cmd_uses_default_model(tmp_path):
    h = OpenCodeHarness(opencode_path="/fake/opencode")
    cmd = h._build_cmd(_make_req(tmp_path))
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == DEFAULT_OPENCODE_MODEL


def test_build_cmd_overrides_model(tmp_path):
    h = OpenCodeHarness(opencode_path="/fake/opencode")
    cmd = h._build_cmd(_make_req(tmp_path, model="anthropic/claude-3-5-sonnet"))
    assert cmd[cmd.index("--model") + 1] == "anthropic/claude-3-5-sonnet"


def test_build_cmd_passes_effort_as_variant(tmp_path):
    h = OpenCodeHarness(opencode_path="/fake/opencode")
    cmd = h._build_cmd(_make_req(tmp_path, effort="high"))
    assert "--variant" in cmd and cmd[cmd.index("--variant") + 1] == "high"


# ---- End-to-end ----


def test_run_turn_happy_path(tmp_path):
    fake_body = textwrap.dedent(
        """
        for ev in [
            {"type":"step_start","sessionID":"ses_xyz","part":{"type":"step-start"}},
            {"type":"text","sessionID":"ses_xyz","part":{"type":"text","text":"Hello!"}},
            {"type":"step_finish","sessionID":"ses_xyz","part":{"type":"step-finish","tokens":{"total":100,"input":50,"output":50,"cache":{"write":0,"read":0}},"cost":0.0042}},
        ]:
            print(json.dumps(ev), flush=True)
        """
    )
    fake = _write_fake(tmp_path, fake_body)
    h = OpenCodeHarness(opencode_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnFinal)
    final = events[-1]
    assert final.session_id == "ses_xyz"
    assert final.raw_text == "Hello!"
    assert final.num_turns == 1
    assert final.total_cost_usd == pytest.approx(0.0042)
    assert any(isinstance(e, TextDelta) and e.text == "Hello!" for e in events)


def test_run_turn_emits_tool_use_and_result(tmp_path):
    fake_body = textwrap.dedent(
        """
        for ev in [
            {"type":"step_start","sessionID":"s","part":{"type":"step-start"}},
            {"type":"tool_use","sessionID":"s","part":{
                "type":"tool","tool":"bash","callID":"call_1",
                "state":{"status":"completed","input":{"command":"echo hi"},
                         "output":"hi\\n"}}},
            {"type":"text","sessionID":"s","part":{"type":"text","text":"Done."}},
            {"type":"step_finish","sessionID":"s","part":{"type":"step-finish","cost":0}},
        ]:
            print(json.dumps(ev), flush=True)
        """
    )
    fake = _write_fake(tmp_path, fake_body)
    h = OpenCodeHarness(opencode_path=str(fake))
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


def test_run_turn_aggregates_multiple_text_events(tmp_path):
    fake_body = textwrap.dedent(
        """
        for ev in [
            {"type":"step_start","sessionID":"s","part":{}},
            {"type":"text","sessionID":"s","part":{"type":"text","text":"First."}},
            {"type":"step_finish","sessionID":"s","part":{"cost":0}},
            {"type":"step_start","sessionID":"s","part":{}},
            {"type":"text","sessionID":"s","part":{"type":"text","text":"Second."}},
            {"type":"step_finish","sessionID":"s","part":{"cost":0}},
        ]:
            print(json.dumps(ev), flush=True)
        """
    )
    fake = _write_fake(tmp_path, fake_body)
    h = OpenCodeHarness(opencode_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnFinal)
    assert events[-1].raw_text == "First.\nSecond."
    assert events[-1].num_turns == 2


def test_run_turn_classifies_rate_limit_via_error_event(tmp_path):
    fake_body = textwrap.dedent(
        """
        for ev in [
            {"type":"step_start","sessionID":"s","part":{}},
            {"type":"error","sessionID":"s","error":{"data":{"message":"429 rate limit hit"}}},
        ]:
            print(json.dumps(ev), flush=True)
        """
    )
    fake = _write_fake(tmp_path, fake_body)
    h = OpenCodeHarness(opencode_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "rate_limit"


def test_run_turn_classifies_unknown_error(tmp_path):
    fake_body = textwrap.dedent(
        """
        for ev in [
            {"type":"step_start","sessionID":"s","part":{}},
            {"type":"error","sessionID":"s","error":{"data":{"message":"Model not found: foo/bar"}}},
        ]:
            print(json.dumps(ev), flush=True)
        """
    )
    fake = _write_fake(tmp_path, fake_body)
    h = OpenCodeHarness(opencode_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "unknown"
    assert "Model not found" in events[-1].message


def test_run_turn_handles_oom_exit(tmp_path):
    fake = _write_fake(tmp_path, "import sys; sys.exit(137)")
    h = OpenCodeHarness(opencode_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "oom"


def test_run_turn_handles_stale_session_in_stderr(tmp_path):
    fake = _write_fake(
        tmp_path,
        textwrap.dedent(
            """
            sys.stderr.write("Session not found: ses_abc\\n")
            sys.exit(1)
            """
        ),
    )
    h = OpenCodeHarness(opencode_path=str(fake))
    events = asyncio.run(
        _drain(h.run_turn(_make_req(tmp_path, resume_session_id="ses_abc")))
    )
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "corrupt_session"


def test_run_turn_timeout(tmp_path):
    fake = _write_fake(tmp_path, "import time; time.sleep(5)")
    h = OpenCodeHarness(opencode_path=str(fake), max_timeout_seconds=0.5)
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "timeout"


def test_run_turn_empty_text_emits_placeholder(tmp_path):
    fake_body = textwrap.dedent(
        """
        for ev in [
            {"type":"step_start","sessionID":"s","part":{}},
            {"type":"step_finish","sessionID":"s","part":{"cost":0}},
        ]:
            print(json.dumps(ev), flush=True)
        """
    )
    fake = _write_fake(tmp_path, fake_body)
    h = OpenCodeHarness(opencode_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnFinal)
    assert "no text response" in events[-1].raw_text


def test_run_turn_falls_back_when_no_events_parsed(tmp_path):
    fake = _write_fake(tmp_path, "print('plain text not json')")
    h = OpenCodeHarness(opencode_path=str(fake))
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnFinal)
    assert "plain text" in events[-1].raw_text


def test_run_turn_calls_progress_per_stdout_line(tmp_path):
    fake_body = textwrap.dedent(
        """
        for ev in [
            {"type":"step_start","sessionID":"s","part":{}},
            {"type":"text","sessionID":"s","part":{"type":"text","text":"a"}},
            {"type":"text","sessionID":"s","part":{"type":"text","text":"b"}},
            {"type":"step_finish","sessionID":"s","part":{"cost":0}},
        ]:
            print(json.dumps(ev), flush=True)
        """
    )
    fake = _write_fake(tmp_path, fake_body)
    counter = {"n": 0}
    h = OpenCodeHarness(
        opencode_path=str(fake),
        on_progress=lambda: counter.__setitem__("n", counter["n"] + 1),
    )
    asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert counter["n"] >= 4


def test_cancel_kills_running_proc(tmp_path):
    fake = _write_fake(tmp_path, "import time; time.sleep(30)")
    h = OpenCodeHarness(opencode_path=str(fake), max_timeout_seconds=10)

    async def run_and_cancel():
        task = asyncio.create_task(_drain(h.run_turn(_make_req(tmp_path))))
        await asyncio.sleep(0.2)
        await h.cancel()
        return await task

    events = asyncio.run(run_and_cancel())
    assert events
