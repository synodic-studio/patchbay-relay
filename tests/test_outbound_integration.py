"""Integration test: outbound notifications injected into Claude system prompt."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import bridge


SESSION_KEY = "99999_42"  # fake session key — never use a real one


def _make_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock(spec=subprocess.Popen)
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 99999
    return proc


def _valid_stdout(text="OK"):
    return json.dumps(
        [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
            {"type": "result", "session_id": "sess-123", "result": text},
        ]
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(bridge, "_sessions", {})


@pytest.fixture(autouse=True)
def _patch_deps():
    with (
        patch("bridge.get_session_id", return_value=None),
        patch("bridge.save_session_id"),
        patch("bridge.clear_session"),
        patch("bridge.get_chat_working_dir", return_value="/tmp/fake"),
        patch("bridge.get_chat_agent", return_value="buddy"),
        patch("bridge._load_chat_projects", return_value={}),
        patch("bridge._parse_project_entry", return_value=(None, None)),
        patch("bridge._log_activity"),
    ):
        yield


class TestOutboundInjection:
    def test_recent_outbound_appears_in_system_prompt(self, tmp_path, monkeypatch):
        """When outbound log has entries, they appear in --append-system-prompt."""
        # Write a fake outbound entry
        monkeypatch.setattr("stargate.outbound.OUTBOUND_DIR", tmp_path)
        from stargate.outbound import log_outbound

        log_outbound(SESSION_KEY, "Your daily digest is ready. 3 new items.", "buddy")

        proc = _make_proc(stdout=_valid_stdout())
        captured_cmd = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return proc

        with patch("bridge.subprocess.Popen", side_effect=fake_popen):
            bridge.run_claude("looks good", SESSION_KEY)

        # Find the --append-system-prompt value
        prompt_idx = captured_cmd.index("--append-system-prompt")
        system_prompt = captured_cmd[prompt_idx + 1]

        assert "RECENT NOTIFICATIONS" in system_prompt
        assert "buddy" in system_prompt
        assert "daily digest" in system_prompt

    def test_no_outbound_no_section(self, tmp_path, monkeypatch):
        """When outbound log is empty, no RECENT NOTIFICATIONS section."""
        monkeypatch.setattr("stargate.outbound.OUTBOUND_DIR", tmp_path)

        proc = _make_proc(stdout=_valid_stdout())
        captured_cmd = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return proc

        with patch("bridge.subprocess.Popen", side_effect=fake_popen):
            bridge.run_claude("hello", SESSION_KEY)

        prompt_idx = captured_cmd.index("--append-system-prompt")
        system_prompt = captured_cmd[prompt_idx + 1]
        assert "RECENT NOTIFICATIONS" not in system_prompt

    def test_limits_to_3_messages(self, tmp_path, monkeypatch):
        """Only the last 3 outbound messages are included."""
        monkeypatch.setattr("stargate.outbound.OUTBOUND_DIR", tmp_path)
        from stargate.outbound import log_outbound

        for i in range(5):
            log_outbound(SESSION_KEY, f"notification {i}", "buddy")

        proc = _make_proc(stdout=_valid_stdout())
        captured_cmd = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return proc

        with patch("bridge.subprocess.Popen", side_effect=fake_popen):
            bridge.run_claude("reply", SESSION_KEY)

        prompt_idx = captured_cmd.index("--append-system-prompt")
        system_prompt = captured_cmd[prompt_idx + 1]

        # Should have notifications 2, 3, 4 (last 3)
        assert "notification 2" in system_prompt
        assert "notification 3" in system_prompt
        assert "notification 4" in system_prompt
        # Should NOT have 0 or 1
        assert "notification 0" not in system_prompt
        assert "notification 1" not in system_prompt
