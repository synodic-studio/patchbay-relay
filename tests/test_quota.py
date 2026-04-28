"""Tests for patchbay.quota — quota detection and Forge handoff."""

import datetime
from unittest.mock import patch


import patchbay.quota
from patchbay.quota import handoff_to_forge, is_quota_error


# ---------------------------------------------------------------------------
# is_quota_error — stderr matching
# ---------------------------------------------------------------------------


class TestIsQuotaErrorStderr:
    """Tests for stderr-based quota detection."""

    def test_matches_please_wait_and_try_again(self):
        assert is_quota_error([], "Please wait and try again later") is True

    def test_matches_rate_limit_in_stderr(self):
        assert is_quota_error([], "You have hit a rate limit") is True

    def test_matches_rate_limit_underscore_in_stderr(self):
        assert is_quota_error([], "Error: rate_limit exceeded") is True

    def test_case_insensitive_stderr(self):
        assert is_quota_error([], "PLEASE WAIT AND TRY AGAIN LATER") is True
        assert is_quota_error([], "Rate Limit Exceeded") is True
        assert is_quota_error([], "RATE_LIMIT hit") is True

    def test_empty_stderr_and_empty_events(self):
        assert is_quota_error([], "") is False

    def test_no_match_in_stderr(self):
        assert is_quota_error([], "Some other error occurred") is False


# ---------------------------------------------------------------------------
# is_quota_error — result event error field matching
# ---------------------------------------------------------------------------


class TestIsQuotaErrorResultEvent:
    """Tests for result event error-field detection."""

    def test_matches_rate_limit_error_in_result(self):
        events = [{"type": "result", "error": "rate_limit_error"}]
        assert is_quota_error(events, "") is True

    def test_matches_overloaded_error_in_result(self):
        events = [{"type": "result", "error": "overloaded_error"}]
        assert is_quota_error(events, "") is True

    def test_multiple_events_only_last_result_checked(self):
        events = [
            {"type": "result", "error": "rate_limit_error"},
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "error": ""},
        ]
        # The last result event has no quota error, but the first does.
        # reversed() finds the last result first — which has empty error.
        assert is_quota_error(events, "") is False

    def test_events_with_no_result_type_only_checks_stderr(self):
        events = [
            {"type": "assistant", "message": {"content": []}},
            {"type": "system", "text": "something"},
        ]
        assert is_quota_error(events, "") is False
        assert is_quota_error(events, "rate limit hit") is True


# ---------------------------------------------------------------------------
# is_quota_error — short assistant text matching
# ---------------------------------------------------------------------------


class TestIsQuotaErrorShortText:
    """Tests for quota patterns in short assistant text."""

    def test_matches_too_many_requests_in_short_text(self):
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "too many requests"}]
                },
            }
        ]
        assert is_quota_error(events, "") is True

    def test_does_not_match_quota_pattern_in_long_text(self):
        long_text = "x" * 300 + " too many requests"
        assert len(long_text) > 300
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": long_text}]
                },
            }
        ]
        assert is_quota_error(events, "") is False

    def test_text_exactly_at_300_char_boundary(self):
        # 300 chars exactly — should still be checked (< 300 is the guard)
        text_299 = "a" * 281 + " too many requests"
        assert len(text_299) < 300
        events_under = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": text_299}]
                },
            }
        ]
        assert is_quota_error(events_under, "") is True

        text_300 = "a" * 282 + " too many requests"
        assert len(text_300) >= 300
        events_at = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": text_300}]
                },
            }
        ]
        assert is_quota_error(events_at, "") is False


# ---------------------------------------------------------------------------
# handoff_to_forge — backtick fence handling
# ---------------------------------------------------------------------------


class TestHandoffToForgeFence:
    """Tests for code-fence escaping in the queue file."""

    def _call_handoff(self, tmp_path, message):
        queue_dir = tmp_path / "forge" / "queue"
        fake_now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        fake_today = datetime.date(2026, 1, 1)

        with (
            patch.object(patchbay.quota, "FORGE_QUEUE_DIR", queue_dir),
            patch("patchbay.quota.datetime") as mock_dt,
            patch("patchbay.quota.log_activity"),
        ):
            mock_dt.datetime.now.return_value = fake_now
            mock_dt.date.today.return_value = fake_today
            mock_dt.timezone = datetime.timezone

            result = handoff_to_forge(
                session_key="test-key",
                message=message,
                chat_id=1,
                thread_id=2,
                session_id=None,
                working_dir="/tmp",
            )

        content = next(queue_dir.iterdir()).read_text()
        return result, content

    def test_message_with_single_backticks(self, tmp_path):
        result, content = self._call_handoff(tmp_path, "Use `foo` here")
        assert result is True
        # Default triple fence is enough (max backtick run is 1, fence = max(3,2) = 3)
        assert "```\nUse `foo` here\n```" in content

    def test_message_with_triple_backticks(self, tmp_path):
        result, content = self._call_handoff(tmp_path, "```python\nprint('hi')\n```")
        assert result is True
        # Fence must be 4 backticks (max run is 3, fence = max(3,4) = 4)
        assert "````\n```python\nprint('hi')\n```\n````" in content

    def test_message_with_five_backticks(self, tmp_path):
        result, content = self._call_handoff(tmp_path, "Here: `````")
        assert result is True
        # Fence must be 6 backticks (max run is 5, fence = max(3,6) = 6)
        assert "``````\nHere: `````\n``````" in content

    def test_empty_message(self, tmp_path):
        result, content = self._call_handoff(tmp_path, "")
        assert result is True
        # No backticks → default triple fence, empty content between fences
        assert "```\n\n```" in content

    def test_very_long_message(self, tmp_path):
        long_msg = "word " * 10000
        result, content = self._call_handoff(tmp_path, long_msg)
        assert result is True
        assert long_msg in content


# ---------------------------------------------------------------------------
# handoff_to_forge — session key sanitization
# ---------------------------------------------------------------------------


class TestHandoffToForgeSessionKey:
    """Tests for filename generation from session_key."""

    def test_session_key_with_hyphens_truncated(self, tmp_path):
        queue_dir = tmp_path / "forge" / "queue"
        fake_now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        fake_today = datetime.date(2026, 1, 1)

        with (
            patch.object(patchbay.quota, "FORGE_QUEUE_DIR", queue_dir),
            patch("patchbay.quota.datetime") as mock_dt,
            patch("patchbay.quota.log_activity"),
        ):
            mock_dt.datetime.now.return_value = fake_now
            mock_dt.date.today.return_value = fake_today
            mock_dt.timezone = datetime.timezone

            result = handoff_to_forge(
                session_key="aaa-bbb-ccc-ddd-eee-fff-ggg",
                message="test",
                chat_id=1,
                thread_id=2,
                session_id=None,
                working_dir="/tmp",
            )

        assert result is True
        queue_file = next(queue_dir.iterdir())
        # Hyphens removed, then truncated to 20 chars
        sanitized = "aaa-bbb-ccc-ddd-eee-fff-ggg".replace("-", "")[:20]
        assert queue_file.name == f"bridge-recovery-{sanitized}.md"


# ---------------------------------------------------------------------------
# handoff_to_forge — directory creation and error handling
# ---------------------------------------------------------------------------


class TestHandoffToForgeFileSystem:
    """Tests for directory creation and write failure handling."""

    def test_creates_parent_directories(self, tmp_path):
        queue_dir = tmp_path / "deep" / "nested" / "forge" / "queue"
        assert not queue_dir.exists()

        fake_now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        fake_today = datetime.date(2026, 1, 1)

        with (
            patch.object(patchbay.quota, "FORGE_QUEUE_DIR", queue_dir),
            patch("patchbay.quota.datetime") as mock_dt,
            patch("patchbay.quota.log_activity"),
        ):
            mock_dt.datetime.now.return_value = fake_now
            mock_dt.date.today.return_value = fake_today
            mock_dt.timezone = datetime.timezone

            result = handoff_to_forge(
                session_key="test",
                message="hello",
                chat_id=1,
                thread_id=2,
                session_id=None,
                working_dir="/tmp",
            )

        assert result is True
        assert queue_dir.exists()
        assert len(list(queue_dir.iterdir())) == 1

    def test_write_failure_returns_false(self, tmp_path):
        # Point to a path where we can't write (a file where a dir should be)
        blocker = tmp_path / "blocker"
        blocker.write_text("I'm a file, not a dir")
        queue_dir = blocker / "queue"

        fake_now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        fake_today = datetime.date(2026, 1, 1)

        with (
            patch.object(patchbay.quota, "FORGE_QUEUE_DIR", queue_dir),
            patch("patchbay.quota.datetime") as mock_dt,
            patch("patchbay.quota.log_activity"),
        ):
            mock_dt.datetime.now.return_value = fake_now
            mock_dt.date.today.return_value = fake_today
            mock_dt.timezone = datetime.timezone

            result = handoff_to_forge(
                session_key="test",
                message="hello",
                chat_id=1,
                thread_id=2,
                session_id=None,
                working_dir="/tmp",
            )

        assert result is False
