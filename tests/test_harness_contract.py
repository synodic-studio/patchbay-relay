"""Harness contract tests.

Validates the Harness protocol contract — exactly one terminator event
(TurnFinal or TurnError) as the last event of the stream — against
ClaudeCliHarness. Uses the same fake-claude factory pattern as
test_chaos_run_claude.py.

These tests exercise the full pipeline (Popen → drain → parse → emit)
against real fake binaries; they are not unit tests with mocked Popen.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import textwrap
from pathlib import Path

from stargate.harness import (
    ClaudeCliHarness,
    Harness,
    TextDelta,
    ToolUse,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)


# ---------------------------------------------------------------------------
# Fake-claude factory (same pattern as test_chaos_run_claude.py)
# ---------------------------------------------------------------------------


def _write_fake(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "fake_claude_harness"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, os, json\n"
        "if __name__ == '__main__':\n"
        + textwrap.indent(body, "    ")
        + "\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _make_request(
    *,
    prompt: str = "hello",
    tmp_path: Path,
    resume_session_id: str | None = None,
) -> TurnRequest:
    return TurnRequest(
        prompt=prompt,
        session_key="test_999",
        project_dir=tmp_path,
        system_prompt="test system prompt",
        resume_session_id=resume_session_id,
        model=None,
        effort=None,
        allowed_tools=None,
        disallowed_tools=["AskUserQuestion"],
        max_turns=5,
        plugin_dir=None,
    )


async def _collect(harness: Harness, req: TurnRequest) -> list[TurnEvent]:
    events: list[TurnEvent] = []
    async for ev in harness.run_turn(req):
        events.append(ev)
    return events


def _has_exactly_one_terminator(events: list[TurnEvent]) -> bool:
    """The contract: exactly one TurnFinal OR TurnError, and it's last."""
    if not events:
        return False
    terminators = [e for e in events if isinstance(e, (TurnFinal, TurnError))]
    if len(terminators) != 1:
        return False
    return isinstance(events[-1], (TurnFinal, TurnError))


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_claude_cli_implements_harness_protocol(self):
        """ClaudeCliHarness is a Harness (runtime_checkable Protocol)."""
        harness = ClaudeCliHarness()
        assert isinstance(harness, Harness)

    def test_capabilities_advertised(self):
        harness = ClaudeCliHarness()
        caps = harness.capabilities
        assert caps.supports_resume is True
        assert caps.supports_mcp is True
        assert caps.supports_effort is True

    def test_name_is_cc_cli(self):
        assert ClaudeCliHarness().name == "cc-cli"


# ---------------------------------------------------------------------------
# Successful turn — events flow + terminator contract
# ---------------------------------------------------------------------------


class TestSuccessfulTurn:
    def test_text_only_response_yields_textdelta_then_turnfinal(self, tmp_path):
        payload = json.dumps([
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello there"}]},
            },
            {"type": "result", "subtype": "success", "session_id": "s-text", "num_turns": 2, "result": "hello there"},
        ])
        fake = _write_fake(tmp_path, f"sys.stdout.write({payload!r})\nsys.exit(0)\n")
        harness = ClaudeCliHarness(claude_path=str(fake), max_timeout_seconds=5, max_turns_default=5)
        req = _make_request(tmp_path=tmp_path)

        events = asyncio.run(_collect(harness, req))

        assert _has_exactly_one_terminator(events)
        assert isinstance(events[-1], TurnFinal)
        assert events[-1].session_id == "s-text"
        assert events[-1].num_turns == 2
        assert "hello there" in events[-1].raw_text

        # At least one TextDelta with the assistant text.
        deltas = [e for e in events if isinstance(e, TextDelta)]
        assert deltas, "expected at least one TextDelta"
        assert any("hello there" in d.text for d in deltas)

    def test_tool_use_blocks_emitted_as_tooluse_events(self, tmp_path):
        payload = json.dumps([
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/tmp/x"}},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "done"}]},
            },
            {"type": "result", "subtype": "success", "session_id": "s-tool", "num_turns": 1, "result": "done"},
        ])
        fake = _write_fake(tmp_path, f"sys.stdout.write({payload!r})\nsys.exit(0)\n")
        harness = ClaudeCliHarness(claude_path=str(fake), max_timeout_seconds=5, max_turns_default=5)

        events = asyncio.run(_collect(harness, _make_request(tmp_path=tmp_path)))

        tool_uses = [e for e in events if isinstance(e, ToolUse)]
        assert len(tool_uses) == 1
        assert tool_uses[0].name == "Read"
        assert tool_uses[0].id == "tu_1"
        assert tool_uses[0].input == {"file_path": "/tmp/x"}

        assert _has_exactly_one_terminator(events)
        assert isinstance(events[-1], TurnFinal)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_oom_137_yields_turnerror_oom_retryable(self, tmp_path):
        fake = _write_fake(tmp_path, "sys.stderr.write('killed\\n')\nsys.exit(137)\n")
        harness = ClaudeCliHarness(claude_path=str(fake), max_timeout_seconds=5)

        events = asyncio.run(_collect(harness, _make_request(tmp_path=tmp_path)))

        assert _has_exactly_one_terminator(events)
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "oom"
        assert term.retryable is True
        assert term.metadata["exit_code"] == 137

    def test_stale_session_yields_corrupt_session_when_resume_provided(self, tmp_path):
        fake = _write_fake(
            tmp_path,
            "sys.stderr.write('No conversation found with session ID xyz\\n')\nsys.exit(1)\n",
        )
        harness = ClaudeCliHarness(claude_path=str(fake), max_timeout_seconds=5)
        req = _make_request(tmp_path=tmp_path, resume_session_id="stale-id-123")

        events = asyncio.run(_collect(harness, req))

        assert _has_exactly_one_terminator(events)
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "corrupt_session"
        assert term.retryable is True
        assert term.metadata["stale_session_id"] == "stale-id-123"

    def test_timeout_yields_turnerror_timeout(self, tmp_path):
        fake = _write_fake(tmp_path, "time.sleep(30)\nsys.exit(0)\n")
        harness = ClaudeCliHarness(claude_path=str(fake), max_timeout_seconds=1)

        events = asyncio.run(_collect(harness, _make_request(tmp_path=tmp_path)))

        assert _has_exactly_one_terminator(events)
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "timeout"
        assert term.retryable is True

    def test_quota_in_stderr_yields_rate_limit(self, tmp_path):
        fake = _write_fake(
            tmp_path,
            "sys.stderr.write('Please wait and try again later\\n')\nsys.exit(1)\n",
        )
        harness = ClaudeCliHarness(claude_path=str(fake), max_timeout_seconds=5)

        events = asyncio.run(_collect(harness, _make_request(tmp_path=tmp_path)))

        assert _has_exactly_one_terminator(events)
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "rate_limit"
        # Quota is non-retryable — bridge does Forge handoff instead.
        assert term.retryable is False

    def test_max_turns_subtype_yields_max_turns_error(self, tmp_path):
        payload = json.dumps([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial work"}]}},
            {"type": "result", "subtype": "max_turns", "session_id": "s-mx", "num_turns": 5, "result": "partial work"},
        ])
        fake = _write_fake(tmp_path, f"sys.stdout.write({payload!r})\nsys.exit(0)\n")
        harness = ClaudeCliHarness(claude_path=str(fake), max_timeout_seconds=5, max_turns_default=5)

        events = asyncio.run(_collect(harness, _make_request(tmp_path=tmp_path)))

        assert _has_exactly_one_terminator(events)
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "max_turns"
        assert term.metadata["session_id"] == "s-mx"

    def test_clean_exit_empty_stdout_yields_unknown(self, tmp_path):
        fake = _write_fake(tmp_path, "sys.exit(0)\n")
        harness = ClaudeCliHarness(claude_path=str(fake), max_timeout_seconds=5)

        events = asyncio.run(_collect(harness, _make_request(tmp_path=tmp_path)))

        assert _has_exactly_one_terminator(events)
        term = events[-1]
        assert isinstance(term, TurnError)
        assert term.kind == "unknown"


# ---------------------------------------------------------------------------
# Cmd construction
# ---------------------------------------------------------------------------


class TestCmdConstruction:
    def test_minimal_cmd_has_required_flags(self, tmp_path):
        harness = ClaudeCliHarness(claude_path="/fake/claude", max_turns_default=42)
        req = _make_request(tmp_path=tmp_path)
        cmd = harness._build_cmd(req)

        assert cmd[0] == "/fake/claude"
        assert "-p" in cmd
        assert req.prompt in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--max-turns" in cmd
        assert "5" in cmd  # from req.max_turns
        assert "--append-system-prompt" in cmd

    def test_resume_id_added_when_present(self, tmp_path):
        harness = ClaudeCliHarness(claude_path="/fake/claude")
        req = _make_request(tmp_path=tmp_path, resume_session_id="abc-123")
        cmd = harness._build_cmd(req)
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "abc-123"

    def test_resume_omitted_when_absent(self, tmp_path):
        harness = ClaudeCliHarness(claude_path="/fake/claude")
        req = _make_request(tmp_path=tmp_path, resume_session_id=None)
        assert "--resume" not in harness._build_cmd(req)

    def test_disallowed_tools_passed_through(self, tmp_path):
        harness = ClaudeCliHarness(claude_path="/fake/claude")
        req = TurnRequest(
            prompt="x",
            session_key="k",
            project_dir=tmp_path,
            system_prompt="",
            resume_session_id=None,
            model=None,
            effort=None,
            allowed_tools=None,
            disallowed_tools=["AskUserQuestion", "EnterPlanMode"],
            max_turns=10,
            plugin_dir=None,
        )
        cmd = harness._build_cmd(req)
        idx = cmd.index("--disallowed-tools")
        assert cmd[idx + 1] == "AskUserQuestion,EnterPlanMode"


# ---------------------------------------------------------------------------
# Stall callback
# ---------------------------------------------------------------------------


class TestProgressCallback:
    def test_on_progress_called_for_each_stdout_line(self, tmp_path):
        # Fake emits multiple lines of stdout.
        fake = _write_fake(
            tmp_path,
            "for i in range(5):\n"
            "    sys.stdout.write(f'line {i}\\n')\n"
            "    sys.stdout.flush()\n"
            "sys.stdout.write(json.dumps([{'type':'result','subtype':'success','session_id':'s','num_turns':1,'result':'done'}]))\n"
            "sys.exit(0)\n",
        )
        progress_calls: list[float] = []
        harness = ClaudeCliHarness(
            claude_path=str(fake),
            max_timeout_seconds=5,
            on_progress=lambda: progress_calls.append(1.0),
        )

        asyncio.run(_collect(harness, _make_request(tmp_path=tmp_path)))

        # At least one progress call per stdout line; thread timing means we
        # don't pin an exact count but should see > 0.
        assert len(progress_calls) >= 5
