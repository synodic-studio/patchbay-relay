"""Comprehensive edge-case tests for stargate.parser module.

Covers _parse_events, _extract_text_from_events, and parse_claude_response
with a focus on unusual inputs, boundary conditions, and subtle behaviors.
"""

import json
from unittest.mock import patch

import pytest

from stargate.parser import _extract_text_from_events, _parse_events, parse_claude_response


# ---------------------------------------------------------------------------
# _parse_events edge cases
# ---------------------------------------------------------------------------


class TestParseEventsEdgeCases:
    """Edge cases for _parse_events."""

    def test_nested_json_array_filters_non_dict_elements(self):
        """A JSON array of arrays should discard non-dict inner elements."""
        # [[{...}]] — the outer parse yields a list whose sole element is a list (not a dict)
        stdout = json.dumps([[{"type": "result"}]])
        result = _parse_events(stdout)
        assert result == []

    def test_array_with_non_dict_elements_filtered(self):
        """Strings, ints, nulls in a JSON array are silently dropped."""
        payload = [
            "hello",
            42,
            None,
            {"type": "assistant"},
            True,
            {"type": "result"},
        ]
        stdout = json.dumps(payload)
        result = _parse_events(stdout)
        assert len(result) == 2
        assert result[0]["type"] == "assistant"
        assert result[1]["type"] == "result"

    def test_single_object_wrapped_in_list(self):
        """A single dict is returned as a one-element list."""
        obj = {"type": "result", "session_id": "abc"}
        stdout = json.dumps(obj)
        result = _parse_events(stdout)
        assert result == [obj]

    def test_single_object_vs_array_of_one(self):
        """Single object and array-of-one produce identical output."""
        obj = {"type": "result", "session_id": "abc"}
        from_single = _parse_events(json.dumps(obj))
        from_array = _parse_events(json.dumps([obj]))
        assert from_single == from_array

    def test_very_long_json(self):
        """JSON payloads over 100KB parse correctly."""
        long_text = "x" * 120_000
        events = [{"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}}]
        stdout = json.dumps(events)
        assert len(stdout) > 100_000
        result = _parse_events(stdout)
        assert len(result) == 1
        assert result[0]["message"]["content"][0]["text"] == long_text

    def test_unicode_content(self):
        """Unicode (emoji, CJK, RTL) in JSON is preserved."""
        texts = [
            "\U0001f600 smiley",
            "\u4f60\u597d world",
            "\u0645\u0631\u062d\u0628\u0627",
            "caf\u00e9",
        ]
        events = [{"type": "assistant", "text": t} for t in texts]
        stdout = json.dumps(events)
        result = _parse_events(stdout)
        assert len(result) == len(texts)
        for parsed, original_text in zip(result, texts):
            assert parsed["text"] == original_text

    def test_trailing_whitespace_and_newlines(self):
        """Leading/trailing whitespace and newlines do not break parsing."""
        obj = {"type": "result"}
        stdout = f"\n\n  {json.dumps(obj)}  \n\n"
        result = _parse_events(stdout)
        assert result == [obj]

    def test_mixed_valid_and_invalid_ndjson_lines(self):
        """Valid NDJSON lines are kept; invalid lines are silently skipped."""
        lines = [
            json.dumps({"type": "system", "seq": 1}),
            "NOT VALID JSON {{{",
            json.dumps({"type": "assistant", "seq": 2}),
            "",  # empty line
            "12345",  # valid JSON but not a dict
            json.dumps({"type": "result", "seq": 3}),
        ]
        stdout = "\n".join(lines)
        result = _parse_events(stdout)
        assert len(result) == 3
        assert [e["seq"] for e in result] == [1, 2, 3]

    def test_empty_lines_in_ndjson(self):
        """Empty and whitespace-only lines between NDJSON objects are ignored."""
        lines = [
            "",
            "  ",
            json.dumps({"type": "result"}),
            "",
            "  ",
        ]
        stdout = "\n".join(lines)
        result = _parse_events(stdout)
        assert len(result) == 1
        assert result[0]["type"] == "result"

    def test_json_with_trailing_comma_falls_through_to_ndjson(self):
        """A JSON array with a trailing comma is invalid; parser falls to NDJSON path."""
        # Trailing comma makes the whole string invalid JSON, but each line
        # individually might or might not be valid JSON.
        stdout = '[{"type":"a"},{"type":"b"},]'
        result = _parse_events(stdout)
        # The whole string fails json.loads, then NDJSON tries the single line
        # which is also invalid JSON — so we get nothing.
        assert result == []

    def test_empty_string_returns_empty_list(self):
        """Empty string produces no events."""
        assert _parse_events("") == []

    def test_whitespace_only_returns_empty_list(self):
        """Whitespace-only string produces no events."""
        assert _parse_events("   \n\t  ") == []

    def test_empty_json_array(self):
        """An empty JSON array '[]' yields no events."""
        assert _parse_events("[]") == []

    def test_empty_json_object(self):
        """An empty JSON object '{}' is returned as a single event."""
        result = _parse_events("{}")
        assert result == [{}]

    def test_ndjson_with_array_lines_ignored(self):
        """NDJSON lines that parse to arrays (not dicts) are skipped."""
        lines = [
            json.dumps([1, 2, 3]),
            json.dumps({"type": "result"}),
        ]
        stdout = "\n".join(lines)
        # First attempt: json.loads succeeds and returns a list — but elements are ints, not dicts.
        # Actually the whole string is two lines, first line is a valid JSON array.
        # json.loads on the whole multi-line string will fail (two root values), so NDJSON path.
        result = _parse_events(stdout)
        assert len(result) == 1
        assert result[0]["type"] == "result"

    def test_ndjson_with_null_line(self):
        """A line containing just 'null' (valid JSON, not a dict) is skipped."""
        stdout = "null\n" + json.dumps({"type": "result"})
        result = _parse_events(stdout)
        assert len(result) == 1

    def test_boolean_json_value(self):
        """A top-level boolean JSON value (not array, not dict) produces empty list."""
        assert _parse_events("true") == []
        assert _parse_events("false") == []

    def test_numeric_json_value(self):
        """A top-level numeric JSON value produces empty list."""
        assert _parse_events("42") == []
        assert _parse_events("3.14") == []

    def test_json_string_value(self):
        """A top-level JSON string value produces empty list (not a dict)."""
        assert _parse_events('"hello"') == []


# ---------------------------------------------------------------------------
# _extract_text_from_events edge cases
# ---------------------------------------------------------------------------


class TestExtractTextFromEventsEdgeCases:
    """Edge cases for _extract_text_from_events."""

    def test_multiple_assistant_events_uses_last(self):
        """When multiple assistant events have text, the last one wins."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "second"}]}},
            {"type": "result", "result": "fallback", "session_id": "s1"},
        ]
        assert _extract_text_from_events(events) == "second"

    def test_assistant_with_empty_content_array(self):
        """Assistant event with content: [] has no text — falls through."""
        events = [
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "result": "fallback text", "session_id": "s1"},
        ]
        assert _extract_text_from_events(events) == "fallback text"

    def test_assistant_with_mixed_tool_use_and_text(self):
        """Only text blocks are extracted; tool_use blocks are ignored."""
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
                        {"type": "text", "text": "here is the result"},
                        {"type": "tool_use", "id": "t2", "name": "read", "input": {}},
                    ],
                },
            },
        ]
        assert _extract_text_from_events(events) == "here is the result"

    def test_multiple_text_blocks_joined_with_newline(self):
        """Multiple text blocks within one assistant are joined with newlines."""
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "paragraph one"},
                        {"type": "text", "text": "paragraph two"},
                        {"type": "text", "text": "paragraph three"},
                    ],
                },
            },
        ]
        result = _extract_text_from_events(events)
        assert result == "paragraph one\nparagraph two\nparagraph three"

    def test_event_with_missing_message_key(self):
        """An assistant event without a 'message' key is skipped."""
        events = [
            {"type": "assistant"},  # no 'message' key at all
            {"type": "result", "result": "fallback"},
        ]
        assert _extract_text_from_events(events) == "fallback"

    def test_event_with_message_none(self):
        """An assistant event with message=None is handled gracefully."""
        events = [
            {"type": "assistant", "message": None},
            {"type": "result", "result": "fallback"},
        ]
        # isinstance(None, dict) is False, so content becomes []
        assert _extract_text_from_events(events) == "fallback"

    def test_empty_events_list(self):
        """Empty events list returns None."""
        assert _extract_text_from_events([]) is None

    def test_only_tool_result_events(self):
        """Events with only tool_result types return None (no assistant, no result)."""
        events = [
            {"type": "tool_result", "tool_use_id": "t1", "content": "output"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "output2"},
        ]
        assert _extract_text_from_events(events) is None

    def test_result_event_with_empty_string_result(self):
        """A result event whose 'result' is an empty string returns None (falsy)."""
        events = [
            {"type": "result", "result": "", "session_id": "s1"},
        ]
        # empty string is falsy, so `if result_text:` fails; returns None
        assert _extract_text_from_events(events) is None

    def test_result_event_with_none_result(self):
        """A result event whose 'result' is None returns None."""
        events = [
            {"type": "result", "result": None, "session_id": "s1"},
        ]
        assert _extract_text_from_events(events) is None

    def test_result_event_without_result_key(self):
        """A result event missing 'result' entirely returns None."""
        events = [
            {"type": "result", "session_id": "s1"},
        ]
        assert _extract_text_from_events(events) is None

    def test_assistant_text_takes_priority_over_result_text(self):
        """Assistant text is preferred over result event's inline text."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "from assistant"}]}},
            {"type": "result", "result": "from result", "session_id": "s1"},
        ]
        assert _extract_text_from_events(events) == "from assistant"

    def test_only_result_event_returns_result_text(self):
        """When there are no assistant events, result event text is used."""
        events = [
            {"type": "result", "result": "only result text", "session_id": "s1"},
        ]
        assert _extract_text_from_events(events) == "only result text"

    def test_content_with_non_dict_elements(self):
        """Non-dict elements in content array are silently skipped."""
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        "just a string",
                        42,
                        None,
                        {"type": "text", "text": "real text"},
                    ],
                },
            },
        ]
        assert _extract_text_from_events(events) == "real text"

    def test_text_block_without_text_key(self):
        """A content block with type=text but missing 'text' key raises KeyError — verify behavior."""
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text"},  # missing 'text' key
                    ],
                },
            },
        ]
        # The list comprehension does c["text"] which would raise KeyError
        with pytest.raises(KeyError):
            _extract_text_from_events(events)

    def test_message_is_a_list_instead_of_dict(self):
        """If 'message' is a list instead of a dict, content becomes [] (no crash)."""
        events = [
            {"type": "assistant", "message": ["unexpected", "list"]},
            {"type": "result", "result": "fallback"},
        ]
        # isinstance(["unexpected", "list"], dict) is False → content = []
        assert _extract_text_from_events(events) == "fallback"

    def test_last_assistant_with_no_text_skipped_earlier_one_found(self):
        """If the last assistant has no text, earlier assistants with text are found."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "good one"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]}},
            {"type": "result", "result": "fallback"},
        ]
        # reversed iteration: second assistant has no text blocks → skip; first assistant has text
        assert _extract_text_from_events(events) == "good one"


# ---------------------------------------------------------------------------
# parse_claude_response edge cases
# ---------------------------------------------------------------------------


class TestParseClaudeResponseEdgeCases:
    """Edge cases for parse_claude_response (main entry point)."""

    @patch("stargate.parser.save_session_id")
    def test_result_with_error_and_text_text_wins(self, mock_save):
        """When both assistant text and result error exist, text is returned (not error)."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "good response"}]}},
            {"type": "result", "result": "", "session_id": "s1", "error": "something broke"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        # text is found from assistant → returned before error check
        assert result == "good response"

    @patch("stargate.parser.save_session_id")
    def test_result_with_error_no_text_returns_error(self, mock_save):
        """When there's no text but result has error, formatted error is returned."""
        events = [
            {"type": "result", "result": "", "session_id": "s1", "error": "rate limit"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result == "(Claude error: rate limit)"

    @patch("stargate.parser.save_session_id")
    def test_max_turns_subtype_without_text(self, mock_save):
        """max_turns with no assistant text returns just the notice."""
        events = [
            {"type": "result", "result": "", "session_id": "s1", "subtype": "max_turns"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert "produced no text response" in result
        assert "Reply to continue" in result

    @patch("stargate.parser.save_session_id")
    def test_max_turns_via_result_subtype_key(self, mock_save):
        """max_turns detected via 'result_subtype' key (alternate field name)."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
            {"type": "result", "result": "", "session_id": "s1", "result_subtype": "max_turns"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert "working" in result
        assert "turn limit" in result

    @patch("stargate.parser.save_session_id")
    def test_unknown_subtype_logged_but_text_returned(self, mock_save):
        """An unknown subtype is logged, but text is still returned normally."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "normal response"}]}},
            {"type": "result", "result": "", "session_id": "s1", "subtype": "something_new"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result == "normal response"

    @patch("stargate.parser.save_session_id")
    def test_multiple_result_events_uses_last(self, mock_save):
        """When multiple result events exist, the last one is used for session_id and subtype."""
        events = [
            {"type": "result", "result": "first", "session_id": "old_session"},
            {"type": "result", "result": "second", "session_id": "new_session"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        # save_session_id called with the last result event's session_id
        mock_save.assert_called_once_with("test_key", "new_session")
        # No assistant event, so falls back to result text — last result event's "result"
        assert result == "second"

    @patch("stargate.parser.save_session_id")
    def test_result_without_session_id_logs_error(self, mock_save):
        """A result event missing session_id triggers error log, not a crash."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "result", "result": "hello"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        # save_session_id should NOT be called (no session_id)
        mock_save.assert_not_called()
        assert result == "hello"

    @patch("stargate.parser.save_session_id")
    def test_very_long_text_response(self, mock_save):
        """Very long text responses (>100KB) are returned intact."""
        long_text = "A" * 150_000
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}},
            {"type": "result", "result": "", "session_id": "s1"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert len(result) == 150_000
        assert result == long_text

    @patch("stargate.parser.save_session_id")
    def test_text_with_newlines_preserved(self, mock_save):
        """Newlines within text content are preserved."""
        text = "line one\nline two\n\nline four"
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
            {"type": "result", "result": "", "session_id": "s1"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result == text

    @patch("stargate.parser.save_session_id")
    def test_text_with_unicode_preserved(self, mock_save):
        """Unicode characters (emoji, CJK) in response are preserved."""
        text = "\U0001f680 Launch \u2014 \u4f60\u597d \u2014 caf\u00e9"
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
            {"type": "result", "result": "", "session_id": "s1"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result == text

    @patch("stargate.parser.save_session_id")
    def test_text_with_control_characters(self, mock_save):
        """Control characters (tabs, carriage returns) in text are preserved."""
        text = "col1\tcol2\r\nrow2"
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
            {"type": "result", "result": "", "session_id": "s1"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result == text

    def test_garbage_input_returned_as_is(self):
        """Non-JSON input is returned stripped (fallback path)."""
        stdout = "some plain text error message"
        result = parse_claude_response(stdout, "test_key")
        assert result == "some plain text error message"

    def test_whitespace_only_returns_no_parseable(self):
        """Whitespace-only input returns the standard fallback message."""
        result = parse_claude_response("  \n\t  ", "test_key")
        assert result == "(no parseable response)"

    def test_empty_string_returns_no_parseable(self):
        """Empty string returns the standard fallback message."""
        result = parse_claude_response("", "test_key")
        assert result == "(no parseable response)"

    @patch("stargate.parser.save_session_id")
    def test_no_result_event_still_extracts_text(self, mock_save):
        """When there is no result event at all, assistant text is still extracted."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "orphan text"}]}},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        mock_save.assert_not_called()
        assert result == "orphan text"

    @patch("stargate.parser.save_session_id")
    def test_result_event_with_no_text_and_no_error_returns_fallback(self, mock_save):
        """Result event with empty result and no error returns the fallback."""
        events = [
            {"type": "result", "result": "", "session_id": "s1"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result == "(no parseable response)"

    @patch("stargate.parser.save_session_id")
    def test_ndjson_format_with_session_save(self, mock_save):
        """NDJSON format triggers session save and text extraction."""
        lines = [
            json.dumps({"type": "system", "data": "init"}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ndjson reply"}]}}),
            json.dumps({"type": "result", "result": "", "session_id": "ndjson_sess"}),
        ]
        stdout = "\n".join(lines)
        result = parse_claude_response(stdout, "test_key")
        mock_save.assert_called_once_with("test_key", "ndjson_sess")
        assert result == "ndjson reply"

    @patch("stargate.parser.save_session_id")
    def test_session_id_none_in_result_triggers_error_log(self, mock_save):
        """Result event with session_id=None triggers error log path."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
            {"type": "result", "result": "", "session_id": None},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        # session_id is None (falsy), so save_session_id should NOT be called
        mock_save.assert_not_called()
        assert result == "ok"

    @patch("stargate.parser.save_session_id")
    def test_max_turns_with_text_appends_notice(self, mock_save):
        """max_turns with existing text appends the notice after the text."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial work done"}]}},
            {"type": "result", "result": "", "session_id": "s1", "subtype": "max_turns"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result.startswith("partial work done")
        assert result.endswith("]")
        assert "turn limit" in result

    @patch("stargate.parser.save_session_id")
    def test_only_system_events_returns_fallback(self, mock_save):
        """Events with only system type (no assistant, no result) return fallback."""
        events = [
            {"type": "system", "data": "initialized"},
            {"type": "system", "data": "ready"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result == "(no parseable response)"

    @patch("stargate.parser.save_session_id")
    def test_error_field_is_dict_not_string(self, mock_save):
        """Error field that is a dict (not string) is still formatted."""
        events = [
            {"type": "result", "result": "", "session_id": "s1", "error": {"code": 429, "msg": "rate limited"}},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result.startswith("(Claude error:")
        assert "429" in result

    @patch("stargate.parser.save_session_id")
    def test_result_with_both_subtype_and_result_subtype_prefers_subtype(self, mock_save):
        """When both 'subtype' and 'result_subtype' are present, 'subtype' wins (or-short-circuit)."""
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {
                "type": "result",
                "result": "",
                "session_id": "s1",
                "subtype": "max_turns",
                "result_subtype": "something_else",
            },
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        # subtype is checked first via `or`, so max_turns notice should appear
        assert "turn limit" in result

    @patch("stargate.parser.save_session_id")
    def test_empty_events_array_returns_fallback(self, mock_save):
        """An empty JSON array '[]' means no events — returns fallback."""
        result = parse_claude_response("[]", "test_key")
        # _parse_events returns [] → fallback to stdout.strip() or "(no parseable response)"
        assert result == "[]"  # stdout.strip() is "[]" which is truthy

    @patch("stargate.parser.save_session_id")
    def test_text_with_json_inside(self, mock_save):
        """Text content that itself contains JSON strings is returned verbatim."""
        text = 'The config is: {"key": "value", "nested": [1,2,3]}'
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
            {"type": "result", "result": "", "session_id": "s1"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result == text

    @patch("stargate.parser.save_session_id")
    def test_multiple_text_blocks_joined(self, mock_save):
        """Multiple text blocks in assistant content are joined with newline separators."""
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Step 1: do this"},
                        {"type": "text", "text": "Step 2: do that"},
                    ],
                },
            },
            {"type": "result", "result": "", "session_id": "s1"},
        ]
        stdout = json.dumps(events)
        result = parse_claude_response(stdout, "test_key")
        assert result == "Step 1: do this\nStep 2: do that"
