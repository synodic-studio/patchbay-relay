"""Tests for patchbay.quota — quota/rate-limit detection."""

from patchbay.quota import is_quota_error


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
