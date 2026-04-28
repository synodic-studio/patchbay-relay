"""Regression tests for stream-json output parsing.

The bridge runs `claude -p --output-format stream-json --verbose`. That
emits NDJSON: one JSON event per line, in real time. The shift from
`--output-format json` (single buffered array at the end) was made to
fix false-positive stall kills — the stall detector relies on stdout
cadence (state.last_event_at), and json-mode buffering meant zero
output for the whole duration of an active turn, tripping the kill.

These tests pin parser behavior against a captured-from-real-claude
sample of stream-json output, so future parser changes don't regress
the bridge's interpretation of live claude output.
"""

from __future__ import annotations

import json

from patchbay.parser import (
    _extract_text_from_events,
    _parse_events,
    parse_claude_response,
)
from patchbay.quota import is_quota_error


# Captured from a real `claude -p "say hi" --output-format stream-json --verbose --max-turns 1`
# invocation on 2026-04-25. Truncated to the essential shape; full payloads
# would be too large for this fixture.
_STREAM_JSON_SUCCESS = "\n".join([
    json.dumps({
        "type": "system",
        "subtype": "init",
        "cwd": "/tmp",
        "session_id": "abc-123",
        "tools": ["Bash", "Read"],
        "model": "claude-opus-4-7",
    }),
    json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed"},
        "session_id": "abc-123",
    }),
    json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi there friend"}],
        },
        "session_id": "abc-123",
    }),
    json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 2969,
        "num_turns": 1,
        "result": "Hi there friend",
        "session_id": "abc-123",
        "total_cost_usd": 0.338,
    }),
])


class TestStreamJsonShape:
    def test_parses_ndjson_into_event_list(self):
        events = _parse_events(_STREAM_JSON_SUCCESS)
        assert len(events) == 4
        types = [e["type"] for e in events]
        assert types == ["system", "rate_limit_event", "assistant", "result"]

    def test_extracts_assistant_text(self):
        events = _parse_events(_STREAM_JSON_SUCCESS)
        text = _extract_text_from_events(events)
        assert text == "Hi there friend"

    def test_session_id_from_result_event(self, tmp_path, monkeypatch):
        import patchbay.sessions as sessions_mod

        monkeypatch.setattr(sessions_mod, "SESSION_DIR", tmp_path)
        # Use a session_key that satisfies SESSION_KEY_RE (alnum/_/-)
        result = parse_claude_response(_STREAM_JSON_SUCCESS, "stream_test_42")
        assert "Hi there friend" in result
        # Session id should have been persisted by the side effect.
        session_file = tmp_path / "stream_test_42.json"
        assert session_file.exists()
        data = json.loads(session_file.read_text())
        assert data["session_id"] == "abc-123"

    def test_skips_unknown_event_types_gracefully(self):
        # `system` and `rate_limit_event` are not handled but must not break parsing.
        events = _parse_events(_STREAM_JSON_SUCCESS)
        # _extract_text_from_events should only touch assistant + result events.
        assert _extract_text_from_events(events) == "Hi there friend"


class TestStreamJsonToolUse:
    """Parser must surface tool_use blocks in stream-json output."""

    def test_tool_use_blocks_visible_in_assistant_events(self):
        payload = "\n".join([
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/x"}},
                    ],
                },
                "session_id": "s",
            }),
            json.dumps({
                "type": "user",  # tool_result wrapped as user message
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}],
                },
                "session_id": "s",
            }),
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "did it"}]},
                "session_id": "s",
            }),
            json.dumps({
                "type": "result",
                "subtype": "success",
                "session_id": "s",
                "num_turns": 1,
                "result": "did it",
            }),
        ])
        events = _parse_events(payload)
        assistant_events = [e for e in events if e.get("type") == "assistant"]
        # Should have both the tool_use turn and the final-text turn.
        assert len(assistant_events) == 2
        # Final text extracted correctly (skips tool_use turn since it has no text blocks).
        assert _extract_text_from_events(events) == "did it"


class TestStreamJsonQuota:
    """Quota detection must still fire on stream-json result events."""

    def test_rate_limit_in_result_error_field(self):
        payload = json.dumps({
            "type": "result",
            "subtype": "error",
            "session_id": "s",
            "error": "rate_limit_error: please slow down",
        })
        events = _parse_events(payload)
        assert is_quota_error(events, "")

    def test_no_false_positive_on_clean_stream(self):
        events = _parse_events(_STREAM_JSON_SUCCESS)
        assert not is_quota_error(events, "")
