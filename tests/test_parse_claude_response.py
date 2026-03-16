#!/usr/bin/env python3
"""Tests for parse_claude_response function in bridge.py."""

from unittest.mock import patch
import sys
import os

# Add the parent directory to sys.path so we can import bridge
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge


class TestParseClaudeResponse:
    """Test parse_claude_response function."""

    def test_json_array_with_assistant_text_block_returns_text(self):
        """Test case 1: JSON array with assistant text block → returns text."""
        stdout = (
            '[{"type":"assistant","message":{"content":[{"type":"text","text":"hello world"}]}},'
            '{"type":"result","result":"hello world","session_id":"abc123","is_error":false}]'
        )
        session_key = "test_session"

        result = bridge.parse_claude_response(stdout, session_key)

        assert result == "hello world"

    def test_is_error_true_result_returns_error_text(self):
        """Test case 2: is_error=true result → returns error text."""
        stdout = '{"type":"result","result":"error occurred","session_id":"xyz","is_error":true}'
        session_key = "test_session"

        result = bridge.parse_claude_response(stdout, session_key)

        assert result == "error occurred"

    def test_empty_output_returns_non_empty_fallback(self):
        """Test case 3: empty output → returns non-empty fallback string (not None, not empty)."""
        stdout = ""
        session_key = "test_session"

        result = bridge.parse_claude_response(stdout, session_key)

        assert isinstance(result, str)
        assert result != ""
        assert result is not None
        # Should return either "(no parseable response)" or the stripped stdout
        assert result == "(no parseable response)"

    def test_ndjson_format_returns_text(self):
        """Test case 4: NDJSON format → returns text."""
        stdout = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"ndjson test"}]}}\n'
            '{"type":"result","result":"ndjson test","session_id":"def456","is_error":false}'
        )
        session_key = "test_session"

        result = bridge.parse_claude_response(stdout, session_key)

        assert result == "ndjson test"

    def test_output_with_only_tool_use_blocks_returns_meaningful_fallback(self):
        """Test case 5: output with only tool_use blocks and no text → returns meaningful fallback."""
        stdout = (
            '[{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tool_1","name":"some_tool","input":{}}]}},'
            '{"type":"result","result":"","session_id":"ghi789","is_error":false}]'
        )
        session_key = "test_session"

        result = bridge.parse_claude_response(stdout, session_key)

        assert isinstance(result, str)
        assert result != ""
        # Should return "(no parseable response)" since there's no text
        assert result == "(no parseable response)"

    def test_save_session_id_called_when_session_id_present(self):
        """Test case 6: save_session_id is called when session_id present in result event."""
        stdout = (
            '[{"type":"assistant","message":{"content":[{"type":"text","text":"test"}]}},'
            '{"type":"result","result":"test","session_id":"session_123","is_error":false}]'
        )
        session_key = "test_session"

        # Mock save_session_id to verify it's called
        with patch("stargate.parser.save_session_id") as mock_save:
            result = bridge.parse_claude_response(stdout, session_key)

            # Verify save_session_id was called with correct arguments
            mock_save.assert_called_once_with(session_key, "session_123")
            assert result == "test"

    def test_garbage_input_returns_fallback(self):
        """Additional test: garbage input returns fallback string."""
        stdout = "not json at all\nstill not json"
        session_key = "test_session"

        result = bridge.parse_claude_response(stdout, session_key)

        assert isinstance(result, str)
        assert result != ""
        # Should return the stripped stdout
        assert result == stdout.strip()

    def test_result_event_with_error_field_returns_error_message(self):
        """Test that result event with error field returns formatted error message."""
        stdout = (
            '[{"type":"result","result":"","session_id":"err123","is_error":false,"error":"rate limit exceeded"}]'
        )
        session_key = "test_session"

        result = bridge.parse_claude_response(stdout, session_key)

        assert result == "(Claude error: rate limit exceeded)"

    def test_max_turns_subtype_appends_notice(self):
        """Test that max_turns subtype appends notice to text."""
        stdout = (
            '[{"type":"assistant","message":{"content":[{"type":"text","text":"reached limit"}]}},'
            '{"type":"result","result":"reached limit","session_id":"max123","is_error":false,"subtype":"max_turns"}]'
        )
        session_key = "test_session"

        result = bridge.parse_claude_response(stdout, session_key)

        assert "reached limit" in result
        assert (
            "[Reached 30-turn limit. Session preserved — reply to continue or check beads for queued tasks.]"
            in result
        )

    def test_no_events_returns_stdout_or_fallback(self):
        """Test that when no events are parsed, returns stdout or fallback."""
        # Test with whitespace-only stdout
        stdout = "   \n   "
        session_key = "test_session"

        result = bridge.parse_claude_response(stdout, session_key)

        assert result == "(no parseable response)"
