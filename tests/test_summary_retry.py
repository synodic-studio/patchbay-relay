"""Tests for the empty-success summary retry path in run_claude.

When claude -p finishes cleanly but produces no final assistant text,
the bridge re-invokes once with a "summarize what you just did" prompt
to give the user a real reply instead of the "(Completed N turns…)"
placeholder.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import bridge
from stargate import parser


SESSION_KEY = "100_5"
MESSAGE = "do the thing"


def _empty_success_stdout(num_turns=59, session_id="sess-abc"):
    """JSON output simulating Claude finishing successfully with no final text."""
    return json.dumps(
        [
            {
                "type": "result",
                "subtype": "success",
                "session_id": session_id,
                "num_turns": num_turns,
                # NOTE: no `result` text field — that's the empty-success case.
            },
        ]
    )


def _normal_success_stdout(text="hi there", session_id="sess-abc"):
    return json.dumps(
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": session_id,
                "result": text,
            },
        ]
    )


def _make_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock(spec=subprocess.Popen)
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 99
    return proc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(bridge, "_sessions", {})


@pytest.fixture(autouse=True)
def _patch_run_claude_deps():
    with (
        patch("bridge.get_session_id", return_value="sess-abc") as mock_get,
        patch("bridge.clear_session"),
        patch("bridge.get_chat_working_dir", return_value="/tmp/fake"),
        patch("bridge.get_chat_agent", return_value=None),
        patch("bridge._load_chat_projects", return_value={}),
        patch("bridge._parse_project_entry", return_value=(None, None)),
        patch("bridge._log_activity"),
    ):
        yield {"get_session_id": mock_get}


class TestEmptySuccessDetection:
    def test_marker_recognized(self):
        msg = "(Completed 59 turns of work but didn't produce a text response. Check agent files for results.)"
        assert parser.is_empty_success_response(msg)

    def test_normal_text_not_recognized(self):
        assert not parser.is_empty_success_response("hello")
        assert not parser.is_empty_success_response("(Claude error: ...)")
        assert not parser.is_empty_success_response("(no parseable response)")


class TestSummaryRetry:
    def test_retry_replaces_empty_placeholder_with_summary(self, _patch_run_claude_deps):
        """Empty-success on the first call → bridge retries with --resume and
        returns the summary text instead of the placeholder."""
        first = _make_proc(stdout=_empty_success_stdout(num_turns=59))

        # Mock the second call (subprocess.run inside _request_summary)
        summary_completed = MagicMock()
        summary_completed.returncode = 0
        summary_completed.stdout = _normal_success_stdout("Did the thing.")

        with (
            patch("bridge.subprocess.Popen", return_value=first),
            patch("bridge.subprocess.run", return_value=summary_completed) as run_mock,
        ):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert "Did the thing." in result
        # The retry was actually invoked
        run_mock.assert_called_once()
        cmd = run_mock.call_args.args[0]
        assert "--resume" in cmd
        assert "sess-abc" in cmd

    def test_no_retry_when_first_call_already_returns_text(self, _patch_run_claude_deps):
        first = _make_proc(stdout=_normal_success_stdout("normal reply"))

        with (
            patch("bridge.subprocess.Popen", return_value=first),
            patch("bridge.subprocess.run") as run_mock,
        ):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result == "normal reply"
        run_mock.assert_not_called()

    def test_summary_retry_failure_falls_back_to_placeholder(self, _patch_run_claude_deps):
        """If the summary retry also yields nothing, the original placeholder
        is what the user sees — no infinite retries."""
        first = _make_proc(stdout=_empty_success_stdout(num_turns=42))
        summary_completed = MagicMock()
        summary_completed.returncode = 0
        summary_completed.stdout = _empty_success_stdout(num_turns=1)

        with (
            patch("bridge.subprocess.Popen", return_value=first),
            patch("bridge.subprocess.run", return_value=summary_completed),
        ):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        # Original "Completed 42 turns" placeholder, not the summary's
        assert parser.is_empty_success_response(result)
        assert "42 turns" in result

    def test_summary_retry_timeout_falls_back(self, _patch_run_claude_deps):
        first = _make_proc(stdout=_empty_success_stdout(num_turns=10))

        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=120)

        with (
            patch("bridge.subprocess.Popen", return_value=first),
            patch("bridge.subprocess.run", side_effect=boom),
        ):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert parser.is_empty_success_response(result)

    def test_summary_retry_skipped_when_no_session_id(self, _patch_run_claude_deps, monkeypatch):
        """Without a stored session_id we can't --resume, so don't try."""
        # First call returns empty-success but parser hasn't saved a session_id.
        # Patch get_session_id to return None on the post-parse lookup.
        # The fixture already returns "sess-abc"; flip it to None mid-flight by
        # patching at the bridge module level after the first call would have
        # saved one. Easier: patch get_session_id to None for ALL calls and
        # assert the retry path was skipped.
        first = _make_proc(stdout=_empty_success_stdout(num_turns=5))
        with (
            patch("bridge.get_session_id", return_value=None),
            patch("bridge.subprocess.Popen", return_value=first),
            patch("bridge.subprocess.run") as run_mock,
        ):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert parser.is_empty_success_response(result)
        run_mock.assert_not_called()
