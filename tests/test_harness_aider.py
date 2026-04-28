"""Tests for patchbay.harness.aider.AiderHarness.

Uses fake-aider binaries that mimic aider's stdout shape (header banner
+ body + footer) to exercise the chrome-stripping and the rest of the
pipeline without burning provider credits.
"""

from __future__ import annotations

import asyncio
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from patchbay.harness import (
    AiderHarness,
    Harness,
    TextDelta,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)
from patchbay.harness.aider import (
    DEFAULT_AIDER_MODEL,
    _parse_cost,
    _strip_aider_chrome,
)


def _write_fake(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "fake_aider"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, os\n"
        "if __name__ == '__main__':\n"
        + textwrap.indent(body, "    ")
        + "\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _make_req(tmp_path: Path, *, prompt="hi there", **kw) -> TurnRequest:
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


_AIDER_BANNER = (
    "Aider v0.86.2\n"
    "Model: openrouter/deepseek/deepseek-chat with diff edit format\n"
    "Git repo: none\n"
    "Repo-map: disabled\n"
    "\n"
)
_AIDER_FOOTER = (
    "\n"
    "Tokens: 2.4k sent, 1 received. Cost: $0.00034 message, $0.00034 session.\n"
)


# ---- Pure helpers ----


def test_strip_aider_chrome_basic():
    stdout = _AIDER_BANNER + "Hello world!\n" + _AIDER_FOOTER
    assert _strip_aider_chrome(stdout) == "Hello world!"


def test_strip_aider_chrome_handles_restored_history_line():
    stdout = (
        "Aider v0.86.2\n"
        "Model: foo/bar\n"
        "Git repo: none\n"
        "Repo-map: disabled\n"
        "Restored previous conversation history.\n"
        "\n"
        "BANANA42\n"
        + _AIDER_FOOTER
    )
    assert _strip_aider_chrome(stdout) == "BANANA42"


def test_strip_aider_chrome_handles_multiline_body():
    body = "Line 1\nLine 2\n\nLine 4 after blank"
    stdout = _AIDER_BANNER + body + "\n" + _AIDER_FOOTER
    assert _strip_aider_chrome(stdout) == body


def test_strip_aider_chrome_returns_empty_for_blank():
    assert _strip_aider_chrome("") == ""
    assert _strip_aider_chrome(_AIDER_BANNER + _AIDER_FOOTER) == ""


def test_strip_aider_chrome_handles_analytics_preamble():
    """Real aider output begins 'Analytics have been ...' + blank + Aider vX."""
    stdout = (
        "Analytics have been permanently disabled.\n"
        "\n"
        + _AIDER_BANNER
        + "OK\n"
        + _AIDER_FOOTER
    )
    assert _strip_aider_chrome(stdout) == "OK"


def test_parse_cost_extracts_session_cost():
    stdout = "Tokens: 2.4k sent, 5 received. Cost: $0.00034 message, $0.00125 session."
    assert _parse_cost(stdout) == pytest.approx(0.00125)


def test_parse_cost_returns_none_when_absent():
    assert _parse_cost("no cost line here") is None


# ---- Capability / protocol ----


def test_aider_harness_is_protocol_conformant():
    h: Harness = AiderHarness()
    assert h.name == "aider"
    assert h.capabilities.supports_resume is True


# ---- Cmd construction ----


def test_build_cmd_includes_quietening_flags(tmp_path):
    h = AiderHarness(
        aider_path="/fake/aider",
        history_dir=tmp_path,
    )
    cmd = h._build_cmd(_make_req(tmp_path), tmp_path / "history.md")
    for flag in (
        "--no-pretty",
        "--no-stream",
        "--yes-always",
        "--no-fancy-input",
        "--no-check-update",
        "--no-show-release-notes",
        "--analytics-disable",
        "--no-git",
    ):
        assert flag in cmd, f"missing {flag} in {cmd}"
    assert cmd[0] == "/fake/aider"


def test_build_cmd_uses_default_model(tmp_path):
    h = AiderHarness(aider_path="/fake/aider", history_dir=tmp_path)
    cmd = h._build_cmd(_make_req(tmp_path), tmp_path / "h.md")
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == DEFAULT_AIDER_MODEL


def test_build_cmd_overrides_model_from_request(tmp_path):
    h = AiderHarness(aider_path="/fake/aider", history_dir=tmp_path)
    cmd = h._build_cmd(_make_req(tmp_path, model="anthropic/claude-3-5-sonnet"), tmp_path / "h.md")
    assert cmd[cmd.index("--model") + 1] == "anthropic/claude-3-5-sonnet"


def test_build_cmd_passes_message_and_history(tmp_path):
    h = AiderHarness(aider_path="/fake/aider", history_dir=tmp_path)
    history = tmp_path / "h.md"
    cmd = h._build_cmd(_make_req(tmp_path, prompt="do a thing"), history)
    assert cmd[cmd.index("--message") + 1] == "do a thing"
    assert cmd[cmd.index("--chat-history-file") + 1] == str(history)


def test_build_cmd_restore_only_when_history_exists(tmp_path):
    h = AiderHarness(aider_path="/fake/aider", history_dir=tmp_path)
    history = tmp_path / "absent.md"

    cmd_no_resume = h._build_cmd(_make_req(tmp_path), history)
    assert "--no-restore-chat-history" in cmd_no_resume
    assert "--restore-chat-history" not in cmd_no_resume

    cmd_resume_no_file = h._build_cmd(
        _make_req(tmp_path, resume_session_id=str(history)), history
    )
    # File doesn't exist yet — don't tell aider to restore.
    assert "--no-restore-chat-history" in cmd_resume_no_file

    history.write_text("# prior\n")
    cmd_resume = h._build_cmd(
        _make_req(tmp_path, resume_session_id=str(history)), history
    )
    assert "--restore-chat-history" in cmd_resume


def test_build_cmd_writes_system_prompt_file(tmp_path):
    h = AiderHarness(aider_path="/fake/aider", history_dir=tmp_path)
    history = tmp_path / "h.md"
    cmd = h._build_cmd(
        _make_req(tmp_path, system_prompt="Be brief."), history
    )
    read_idx = cmd.index("--read")
    sp_path = Path(cmd[read_idx + 1])
    assert sp_path.exists()
    assert sp_path.read_text() == "Be brief."


def test_resolve_history_path_uses_resume_when_inside_history_dir(tmp_path):
    h = AiderHarness(aider_path="/fake/aider", history_dir=tmp_path)
    explicit = tmp_path / "abc.md"
    req = _make_req(tmp_path, resume_session_id=str(explicit))
    assert h._resolve_history_path(req) == explicit


def test_resolve_history_path_derives_from_session_key_when_resume_outside(tmp_path):
    h = AiderHarness(aider_path="/fake/aider", history_dir=tmp_path)
    req = _make_req(
        tmp_path,
        session_key="-100123456789_42",
        resume_session_id="/some/other/dir/file.md",
    )
    path = h._resolve_history_path(req)
    assert path.parent == tmp_path
    assert "100123456789_42" in path.name


# ---- End-to-end ----


def test_run_turn_happy_path(tmp_path):
    fake_body = textwrap.dedent(
        f"""
        sys.stdout.write({_AIDER_BANNER!r})
        sys.stdout.write("Hello, here is the response\\n")
        sys.stdout.write({_AIDER_FOOTER!r})
        sys.stdout.flush()
        """
    )
    fake = _write_fake(tmp_path, fake_body)
    h = AiderHarness(aider_path=str(fake), history_dir=tmp_path)
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnFinal)
    assert events[-1].raw_text == "Hello, here is the response"
    assert events[-1].total_cost_usd == pytest.approx(0.00034)
    assert any(isinstance(e, TextDelta) for e in events)


def test_run_turn_classifies_rate_limit(tmp_path):
    fake_body = textwrap.dedent(
        f"""
        sys.stdout.write({_AIDER_BANNER!r})
        sys.stdout.write("litellm.RateLimitError: rate limit hit\\n")
        sys.stdout.write({_AIDER_FOOTER!r})
        sys.stdout.flush()
        """
    )
    fake = _write_fake(tmp_path, fake_body)
    h = AiderHarness(aider_path=str(fake), history_dir=tmp_path)
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "rate_limit"


def test_run_turn_handles_no_output_oom(tmp_path):
    fake = _write_fake(tmp_path, "sys.exit(137)")
    h = AiderHarness(aider_path=str(fake), history_dir=tmp_path)
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "oom"


def test_run_turn_handles_nonzero_exit_with_no_body(tmp_path):
    fake = _write_fake(
        tmp_path,
        textwrap.dedent(
            f"""
            sys.stdout.write({_AIDER_BANNER!r})
            sys.stdout.flush()
            sys.stderr.write("aider crashed\\n")
            sys.exit(1)
            """
        ),
    )
    h = AiderHarness(aider_path=str(fake), history_dir=tmp_path)
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "unknown"
    assert events[-1].metadata["exit_code"] == 1


def test_run_turn_timeout(tmp_path):
    fake = _write_fake(tmp_path, "import time; time.sleep(5)")
    h = AiderHarness(
        aider_path=str(fake),
        history_dir=tmp_path,
        max_timeout_seconds=0.5,
    )
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert isinstance(events[-1], TurnError)
    assert events[-1].kind == "timeout"


def test_run_turn_calls_progress_per_stdout_line(tmp_path):
    fake = _write_fake(
        tmp_path,
        textwrap.dedent(
            f"""
            sys.stdout.write({_AIDER_BANNER!r})
            for i in range(3):
                sys.stdout.write(f"line {{i}}\\n")
                sys.stdout.flush()
            sys.stdout.write({_AIDER_FOOTER!r})
            """
        ),
    )
    counter = {"n": 0}
    h = AiderHarness(
        aider_path=str(fake),
        history_dir=tmp_path,
        on_progress=lambda: counter.__setitem__("n", counter["n"] + 1),
    )
    asyncio.run(_drain(h.run_turn(_make_req(tmp_path))))
    assert counter["n"] >= 3


def test_run_turn_session_id_is_history_path(tmp_path):
    fake = _write_fake(
        tmp_path,
        textwrap.dedent(
            f"""
            sys.stdout.write({_AIDER_BANNER!r})
            sys.stdout.write("ok\\n")
            sys.stdout.write({_AIDER_FOOTER!r})
            """
        ),
    )
    h = AiderHarness(aider_path=str(fake), history_dir=tmp_path)
    events = asyncio.run(_drain(h.run_turn(_make_req(tmp_path, session_key="abc"))))
    assert isinstance(events[-1], TurnFinal)
    assert events[-1].session_id is not None
    assert "abc" in events[-1].session_id


def test_cancel_kills_running_proc(tmp_path):
    fake = _write_fake(tmp_path, "import time; time.sleep(30)")
    h = AiderHarness(
        aider_path=str(fake), history_dir=tmp_path, max_timeout_seconds=10
    )

    async def run_and_cancel():
        task = asyncio.create_task(_drain(h.run_turn(_make_req(tmp_path))))
        await asyncio.sleep(0.2)
        await h.cancel()
        return await task

    events = asyncio.run(run_and_cancel())
    assert events
