"""Chaos test for run_claude.

Invokes run_claude against a real fake `claude` binary that emits each of
the failure modes Stargate has historically had to recover from:
partial JSON, slow drip, stderr-only, hang, exit 0 with empty stdout, and
exit 137 (OOM-style). Asserts that every case produces a user-visible
response (never silence) and that no subprocess leaks.

See `docs/STARGATE-IMPROVEMENT-PLAN.md` §3e (CTB-dnc).
"""

from __future__ import annotations

import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import bridge
import stargate.sessions

SESSION_KEY = "chaos_555"
MESSAGE = "trigger chaos"


# ---------------------------------------------------------------------------
# Fake-claude factory — writes a Python script we point CLAUDE_PATH at.
# ---------------------------------------------------------------------------


def _write_fake(tmp_path: Path, body: str) -> Path:
    """Materialize an executable Python script that impersonates `claude`.

    The body is dropped into `if __name__ == "__main__":`, sees argv[1:]
    as the bridge constructed it, and can write to stdout/stderr and exit
    with any code. Made executable for direct exec by subprocess.Popen.
    """
    fake = tmp_path / "fake_claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, os, json\n"
        "if __name__ == '__main__':\n"
        + textwrap.indent(body, "    ")
        + "\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Point bridge state at tmp_path and stub out side-effecting helpers."""
    monkeypatch.setattr(bridge, "_sessions", {})
    monkeypatch.setattr(bridge, "_log_activity", lambda *a, **kw: None)
    monkeypatch.setattr(bridge, "get_chat_working_dir", lambda key: str(tmp_path))
    monkeypatch.setattr(bridge, "get_chat_agent", lambda key: None)
    monkeypatch.setattr(bridge, "_load_chat_projects", lambda: {})
    monkeypatch.setattr(bridge, "_parse_project_entry", lambda e: (None, None))
    monkeypatch.setattr(bridge, "get_chat_model", lambda key: None)
    monkeypatch.setattr(bridge, "resolve_effort", lambda key: "medium")
    monkeypatch.setattr(bridge, "get_recent_outbound", lambda key, max_age=86400.0: [])
    monkeypatch.setattr(bridge, "get_session_id", lambda key: None)
    monkeypatch.setattr(bridge, "clear_session", lambda key: None)
    monkeypatch.setattr(bridge, "consume_stall_kill", lambda key: None)
    monkeypatch.setattr(bridge, "PA_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "MAX_TURNS", 5)
    # Sessions dir for parse_claude_response side-effects.
    monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path)
    yield


def _no_orphan_procs(fake_path: Path) -> bool:
    """Cross-platform best-effort orphan check: no live process holds fake_path open."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-fa", str(fake_path)], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return True  # pgrep returns 1 when no match
    except FileNotFoundError:
        return True  # no pgrep on platform
    return not out.strip()


# ---------------------------------------------------------------------------
# Chaos cases
# ---------------------------------------------------------------------------


class TestChaosRunClaude:
    def test_partial_json_emits_user_visible_response(self, monkeypatch, tmp_path):
        """Fake emits a truncated JSON array. run_claude must not crash."""
        fake = _write_fake(
            tmp_path,
            'sys.stdout.write(\'[{"type":"assist\')\n'  # truncated
            "sys.stdout.flush()\n"
            "sys.exit(0)\n",
        )
        monkeypatch.setattr(bridge, "CLAUDE_PATH", str(fake))

        result = bridge.run_claude(MESSAGE, SESSION_KEY)
        assert isinstance(result, str)
        assert result  # non-empty
        assert _no_orphan_procs(fake)

    def test_slow_drip_byte_at_a_time_completes(self, monkeypatch, tmp_path):
        """Fake writes a valid JSON one character at a time. Tests that
        Popen.communicate() drains the pipe correctly even on slow output."""
        fake = _write_fake(
            tmp_path,
            'payload = json.dumps([{"type":"result","session_id":"s-drip","result":"drip ok"}])\n'
            "for ch in payload:\n"
            "    sys.stdout.write(ch)\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.001)\n"
            "sys.exit(0)\n",
        )
        monkeypatch.setattr(bridge, "CLAUDE_PATH", str(fake))

        result = bridge.run_claude(MESSAGE, SESSION_KEY)
        assert "drip ok" in result
        assert _no_orphan_procs(fake)

    def test_stderr_only_returns_visible_message(self, monkeypatch, tmp_path):
        """Fake writes only to stderr and exits non-zero. run_claude
        must surface the error to the user, not silently swallow it."""
        fake = _write_fake(
            tmp_path,
            "sys.stderr.write('something went wrong\\n')\n"
            "sys.exit(2)\n",
        )
        monkeypatch.setattr(bridge, "CLAUDE_PATH", str(fake))

        result = bridge.run_claude(MESSAGE, SESSION_KEY)
        assert isinstance(result, str)
        assert "something went wrong" in result or "no output" in result
        assert _no_orphan_procs(fake)

    def test_hang_is_killed_by_timeout(self, monkeypatch, tmp_path):
        """Fake hangs longer than MAX_TIMEOUT. run_claude must kill it,
        return a timeout message, and leak no process."""
        fake = _write_fake(
            tmp_path,
            "time.sleep(30)\n"  # well past our short timeout
            "sys.exit(0)\n",
        )
        monkeypatch.setattr(bridge, "CLAUDE_PATH", str(fake))
        monkeypatch.setattr(bridge, "MAX_TIMEOUT", 2)  # 2-second deadline

        result = bridge.run_claude(MESSAGE, SESSION_KEY)
        assert "Timed out" in result
        assert _no_orphan_procs(fake)

    def test_clean_exit_empty_stdout(self, monkeypatch, tmp_path):
        """Fake exits 0 with no stdout. run_claude returns the
        "(no output)" placeholder rather than silence."""
        fake = _write_fake(tmp_path, "sys.exit(0)\n")
        monkeypatch.setattr(bridge, "CLAUDE_PATH", str(fake))

        result = bridge.run_claude(MESSAGE, SESSION_KEY)
        assert result.startswith("(no output")
        assert _no_orphan_procs(fake)

    def test_oom_exit_137(self, monkeypatch, tmp_path):
        """Fake exits with code 137 (OOM-killed). Must produce a visible
        message and not crash the bridge."""
        fake = _write_fake(
            tmp_path,
            "sys.stderr.write('killed\\n')\n"
            "sys.exit(137)\n",
        )
        monkeypatch.setattr(bridge, "CLAUDE_PATH", str(fake))

        result = bridge.run_claude(MESSAGE, SESSION_KEY)
        assert isinstance(result, str)
        assert result  # non-empty
        # Either echoes stderr or returns the no-output placeholder.
        assert "killed" in result or "no output" in result
        assert _no_orphan_procs(fake)

    def test_invalid_json_array_with_garbage(self, monkeypatch, tmp_path):
        """Fake emits non-JSON garbage on stdout. The parser must not crash."""
        fake = _write_fake(
            tmp_path,
            "sys.stdout.write('this is not json at all <<<>>>\\n')\n"
            "sys.exit(0)\n",
        )
        monkeypatch.setattr(bridge, "CLAUDE_PATH", str(fake))

        result = bridge.run_claude(MESSAGE, SESSION_KEY)
        assert isinstance(result, str)
        assert result
        assert _no_orphan_procs(fake)

    def test_no_active_proc_left_after_chaos_run(self, monkeypatch, tmp_path):
        """After every chaos invocation, _sessions[key].proc must be None
        so the stall detector and shutdown paths don't trip on a stale handle."""
        fake = _write_fake(tmp_path, "sys.exit(0)\n")
        monkeypatch.setattr(bridge, "CLAUDE_PATH", str(fake))

        bridge.run_claude(MESSAGE, SESSION_KEY)
        state = bridge._sessions.get(SESSION_KEY)
        if state is not None:
            assert state.proc is None
            assert state.last_event_at is None
