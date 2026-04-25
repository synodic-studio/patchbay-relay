"""Tests for bridge.run_claude — the core Claude CLI invocation function.

Covers command construction, process lifecycle, timeout handling,
stale session retry, quota detection, and output parsing paths.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import bridge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SESSION_KEY = "123_456"
MESSAGE = "Hello Claude"


def _make_proc(stdout="", stderr="", returncode=0):
    """Build a mock Popen whose communicate() returns the given strings."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 99999
    return proc


def _valid_json_stdout(text="Hi there", session_id="sess-abc-123"):
    """Return a JSON stdout string that parse_claude_response can handle."""
    return json.dumps(
        [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": text}],
                },
            },
            {
                "type": "result",
                "session_id": session_id,
                "result": text,
            },
        ]
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_bridge_state(monkeypatch):
    """Reset mutable module-level dicts so tests don't leak into each other."""
    monkeypatch.setattr(bridge, "_sessions", {})


@pytest.fixture(autouse=True)
def _patch_dependencies():
    """Patch all external dependencies that run_claude touches.

    Every test gets these mocks; individual tests can override as needed.

    `_read_proc_streaming` is proxied to `proc.communicate(timeout=...)` so
    tests written for the old `proc.communicate.return_value` / `side_effect`
    pattern keep working after the streaming-reader refactor.
    """

    def _fake_read_streaming(proc, _state, timeout):
        return proc.communicate(timeout=timeout)

    with (
        patch("bridge.get_session_id", return_value=None) as mock_get_session,
        patch("bridge.clear_session") as mock_clear_session,
        patch("bridge.get_chat_working_dir", return_value="/tmp/fake") as mock_get_cwd,
        patch("bridge.get_chat_agent", return_value=None) as mock_get_agent,
        patch("bridge._load_chat_projects", return_value={}) as mock_load_projects,
        patch("bridge._parse_project_entry", return_value=(None, None)) as mock_parse_entry,
        patch("bridge._log_activity") as mock_log_activity,
        patch("bridge._read_proc_streaming", side_effect=_fake_read_streaming),
    ):
        yield {
            "get_session_id": mock_get_session,
            "clear_session": mock_clear_session,
            "get_chat_working_dir": mock_get_cwd,
            "get_chat_agent": mock_get_agent,
            "_load_chat_projects": mock_load_projects,
            "_parse_project_entry": mock_parse_entry,
            "_log_activity": mock_log_activity,
        }


# ---------------------------------------------------------------------------
# 1. Normal response
# ---------------------------------------------------------------------------


class TestNormalResponse:
    """Popen returns valid JSON stdout; parse_claude_response extracts text."""

    def test_returns_parsed_text(self):
        stdout = _valid_json_stdout("Everything is fine")
        proc = _make_proc(stdout=stdout)

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result == "Everything is fine"

    def test_parse_claude_response_is_invoked(self):
        stdout = _valid_json_stdout("Parsed OK")
        proc = _make_proc(stdout=stdout)

        with (
            patch("bridge.subprocess.Popen", return_value=proc),
            patch("bridge.parse_claude_response", return_value="Parsed OK") as mock_parse,
        ):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        mock_parse.assert_called_once_with(stdout, SESSION_KEY)
        assert result == "Parsed OK"


# ---------------------------------------------------------------------------
# 2. Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    """communicate() raises TimeoutExpired; process is killed, message returned."""

    def test_returns_timeout_message(self):
        proc = _make_proc()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=2700),
            ("", ""),  # drain pipes after kill
        ]

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert "Timed out" in result
        assert "min" in result

    def test_proc_kill_called(self):
        proc = _make_proc()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=2700),
            ("", ""),
        ]

        with patch("bridge.subprocess.Popen", return_value=proc):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Stale session
# ---------------------------------------------------------------------------


class TestStaleSession:
    """Empty stdout + 'No conversation found' in stderr + existing session_id
    triggers clear_session + recursive retry.
    """

    def test_clears_session_and_retries(self, _patch_dependencies):
        deps = _patch_dependencies

        # First call: has a session_id, gets stale error
        # Second call (retry): no session_id, succeeds
        deps["get_session_id"].side_effect = ["sess-stale-old", None]

        proc_stale = _make_proc(stdout="", stderr="Error: No conversation found for id")
        proc_ok = _make_proc(stdout=_valid_json_stdout("Retry succeeded"))

        procs = iter([proc_stale, proc_ok])

        with patch("bridge.subprocess.Popen", side_effect=lambda *a, **kw: next(procs)):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        deps["clear_session"].assert_called_once_with(SESSION_KEY)
        assert result == "Retry succeeded"

    def test_no_retry_without_session_id(self, _patch_dependencies):
        """Stale-session retry requires an existing session_id."""
        deps = _patch_dependencies
        deps["get_session_id"].return_value = None

        proc = _make_proc(stdout="", stderr="No conversation found")

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        deps["clear_session"].assert_not_called()
        # Falls through to the empty-output path with stderr
        assert "no output" in result.lower()


# ---------------------------------------------------------------------------
# 4. Empty output
# ---------------------------------------------------------------------------


class TestEmptyOutput:
    """No stdout → "(no output)" or "(no output. stderr: ...)"."""

    def test_no_stdout_no_stderr(self):
        proc = _make_proc(stdout="", stderr="")

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result == "(no output)"

    def test_no_stdout_with_stderr(self):
        proc = _make_proc(stdout="", stderr="something went wrong")

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result == "(no output. stderr: something went wrong)"

    def test_whitespace_only_stdout_treated_as_empty(self):
        proc = _make_proc(stdout="   \n  ", stderr="")

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result == "(no output)"

    def test_stderr_truncated_to_500_chars(self):
        long_stderr = "x" * 1000
        proc = _make_proc(stdout="", stderr=long_stderr)

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        # The stderr in the response should be capped at 500 chars
        assert len(result) < 600
        assert result.startswith("(no output. stderr: ")


# ---------------------------------------------------------------------------
# 5. Quota detection
# ---------------------------------------------------------------------------


class TestQuotaDetection:
    """Quota errors detected via stderr or events return QUOTA_HIT_PREFIX + message."""

    def test_empty_stdout_quota_stderr(self):
        proc = _make_proc(stdout="", stderr="Please wait and try again later")

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result.startswith(bridge.QUOTA_HIT_PREFIX)
        assert result == bridge.QUOTA_HIT_PREFIX + MESSAGE

    def test_valid_stdout_with_quota_events(self):
        """Even if stdout has content, quota detected in events returns prefix."""
        events = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "partial"}]},
            },
            {
                "type": "result",
                "session_id": "sess-quota",
                "error": "rate_limit_error",
            },
        ]
        stdout = json.dumps(events)
        proc = _make_proc(stdout=stdout)

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result.startswith(bridge.QUOTA_HIT_PREFIX)
        assert result == bridge.QUOTA_HIT_PREFIX + MESSAGE

    def test_quota_stderr_rate_limit_variant(self):
        proc = _make_proc(stdout="", stderr="rate limit exceeded")

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result.startswith(bridge.QUOTA_HIT_PREFIX)

    def test_non_quota_stderr_not_treated_as_quota(self):
        proc = _make_proc(stdout="", stderr="some random error")

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert not result.startswith(bridge.QUOTA_HIT_PREFIX)


# ---------------------------------------------------------------------------
# 6. Command construction
# ---------------------------------------------------------------------------


class TestCommandConstruction:
    """Verify the CLI command array built by run_claude."""

    def test_no_resume_flag_without_session_id(self, _patch_dependencies):
        deps = _patch_dependencies
        deps["get_session_id"].return_value = None

        proc = _make_proc(stdout=_valid_json_stdout())
        captured_cmd = None

        def capture_popen(cmd, **kwargs):
            nonlocal captured_cmd
            captured_cmd = cmd
            return proc

        with patch("bridge.subprocess.Popen", side_effect=capture_popen):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        assert "--resume" not in captured_cmd

    def test_resume_flag_with_session_id(self, _patch_dependencies):
        deps = _patch_dependencies
        deps["get_session_id"].return_value = "sess-existing-123"

        proc = _make_proc(stdout=_valid_json_stdout())
        captured_cmd = None

        def capture_popen(cmd, **kwargs):
            nonlocal captured_cmd
            captured_cmd = cmd
            return proc

        with patch("bridge.subprocess.Popen", side_effect=capture_popen):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        idx = captured_cmd.index("--resume")
        assert captured_cmd[idx + 1] == "sess-existing-123"

    def test_agent_mode_in_system_prompt(self, _patch_dependencies):
        deps = _patch_dependencies
        deps["get_chat_agent"].return_value = "iron-temple"

        proc = _make_proc(stdout=_valid_json_stdout())
        captured_cmd = None

        def capture_popen(cmd, **kwargs):
            nonlocal captured_cmd
            captured_cmd = cmd
            return proc

        with patch("bridge.subprocess.Popen", side_effect=capture_popen):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        # The system prompt is the argument after --append-system-prompt
        idx = captured_cmd.index("--append-system-prompt")
        system_prompt = captured_cmd[idx + 1]
        assert "AGENT MODE" in system_prompt
        assert "iron-temple" in system_prompt

    def test_no_agent_mode_without_agent(self, _patch_dependencies):
        deps = _patch_dependencies
        deps["get_chat_agent"].return_value = None

        proc = _make_proc(stdout=_valid_json_stdout())
        captured_cmd = None

        def capture_popen(cmd, **kwargs):
            nonlocal captured_cmd
            captured_cmd = cmd
            return proc

        with patch("bridge.subprocess.Popen", side_effect=capture_popen):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        idx = captured_cmd.index("--append-system-prompt")
        system_prompt = captured_cmd[idx + 1]
        assert "AGENT MODE" not in system_prompt

    def test_message_is_second_argument(self):
        """The -p flag should be followed by the user message."""
        proc = _make_proc(stdout=_valid_json_stdout())
        captured_cmd = None

        def capture_popen(cmd, **kwargs):
            nonlocal captured_cmd
            captured_cmd = cmd
            return proc

        with patch("bridge.subprocess.Popen", side_effect=capture_popen):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        idx = captured_cmd.index("-p")
        assert captured_cmd[idx + 1] == MESSAGE

    def test_cwd_passed_to_popen(self, _patch_dependencies):
        deps = _patch_dependencies
        deps["get_chat_working_dir"].return_value = "/home/test/project"

        proc = _make_proc(stdout=_valid_json_stdout())
        captured_kwargs = {}

        def capture_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return proc

        with patch("bridge.subprocess.Popen", side_effect=capture_popen):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        assert captured_kwargs["cwd"] == "/home/test/project"

    def test_output_format_stream_json(self):
        """stream-json (with --verbose) — needed for stall-detector cadence.

        See the long comment in bridge.run_claude: plain `json` mode buffers
        until the run ends, which produces false-positive stall kills on
        long-but-progressing turns.
        """
        proc = _make_proc(stdout=_valid_json_stdout())
        captured_cmd = None

        def capture_popen(cmd, **kwargs):
            nonlocal captured_cmd
            captured_cmd = cmd
            return proc

        with patch("bridge.subprocess.Popen", side_effect=capture_popen):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        idx = captured_cmd.index("--output-format")
        assert captured_cmd[idx + 1] == "stream-json"
        assert "--verbose" in captured_cmd


# ---------------------------------------------------------------------------
# 7. Process tracking
# ---------------------------------------------------------------------------


class TestProcessTracking:
    """_active_procs is set during execution and cleared after."""

    def test_proc_registered_during_execution(self):
        """While communicate() is running, _active_procs should contain the proc."""
        proc = _make_proc(stdout=_valid_json_stdout())
        recorded_state = {}

        original_communicate = proc.communicate

        def spy_communicate(**kwargs):
            # Snapshot the state while "inside" communicate
            recorded_state["active"] = {k: s.proc for k, s in bridge._sessions.items() if s.proc is not None}
            recorded_state["last_active"] = {k: s.last_event_at for k, s in bridge._sessions.items() if s.last_event_at is not None}
            return original_communicate(**kwargs)

        proc.communicate = spy_communicate

        with patch("bridge.subprocess.Popen", return_value=proc):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        assert SESSION_KEY in recorded_state["active"]
        assert recorded_state["active"][SESSION_KEY] is proc
        assert SESSION_KEY in recorded_state["last_active"]

    def _proc_cleared(self, key):
        state = bridge._sessions.get(key)
        return state is None or state.proc is None

    def test_proc_cleared_after_success(self):
        proc = _make_proc(stdout=_valid_json_stdout())

        with patch("bridge.subprocess.Popen", return_value=proc):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        assert self._proc_cleared(SESSION_KEY)
        assert (bridge._sessions.get(SESSION_KEY) is None or bridge._sessions[SESSION_KEY].last_event_at is None)

    def test_proc_cleared_after_timeout(self):
        proc = _make_proc()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=2700),
            ("", ""),
        ]

        with patch("bridge.subprocess.Popen", return_value=proc):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        assert self._proc_cleared(SESSION_KEY)
        assert (bridge._sessions.get(SESSION_KEY) is None or bridge._sessions[SESSION_KEY].last_event_at is None)

    def test_proc_cleared_after_empty_output(self):
        proc = _make_proc(stdout="", stderr="")

        with patch("bridge.subprocess.Popen", return_value=proc):
            bridge.run_claude(MESSAGE, SESSION_KEY)

        assert self._proc_cleared(SESSION_KEY)
        assert (bridge._sessions.get(SESSION_KEY) is None or bridge._sessions[SESSION_KEY].last_event_at is None)


# ---------------------------------------------------------------------------
# 8. Non-zero exit code
# ---------------------------------------------------------------------------


class TestNonZeroExitCode:
    """Non-zero exit with stderr is logged; no-parseable-response surfaces error."""

    def test_nonzero_exit_with_stderr_still_returns_parsed(self):
        """If stdout is parseable, the response is returned even on non-zero exit."""
        stdout = _valid_json_stdout("Got a response despite error")
        proc = _make_proc(stdout=stdout, stderr="warning: something", returncode=1)

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result == "Got a response despite error"

    def test_no_parseable_response_with_nonzero_exit_surfaces_stderr(self):
        """If parsing yields nothing and exit code != 0, the stderr is surfaced."""
        # Events that parse but have no extractable text and no result error
        # → parse_claude_response returns "(no parseable response)"
        stdout = json.dumps([{"type": "system", "data": "something"}])
        proc = _make_proc(stdout=stdout, stderr="fatal: crash", returncode=1)

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result == "(Claude exited with error: fatal: crash)"

    def test_no_parseable_response_with_zero_exit_returns_raw(self):
        """If exit code is 0 but parsing fails, the raw no-parseable-response is returned."""
        stdout = "not valid json at all"
        proc = _make_proc(stdout=stdout, returncode=0)

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        # parse_claude_response falls back to stdout.strip() for non-JSON
        assert result == "not valid json at all"

    def test_nonzero_exit_no_stderr_returns_parsed(self):
        """Non-zero exit with empty stderr should still return parsed response."""
        stdout = _valid_json_stdout("Response text here")
        proc = _make_proc(stdout=stdout, stderr="", returncode=2)

        with patch("bridge.subprocess.Popen", return_value=proc):
            result = bridge.run_claude(MESSAGE, SESSION_KEY)

        assert result == "Response text here"
