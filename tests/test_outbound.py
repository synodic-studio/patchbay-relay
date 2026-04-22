"""Tests for stargate.outbound — outbound notification logging."""

import json
import time

import pytest

from stargate.outbound import (
    MAX_ENTRIES,
    get_recent_outbound,
    log_outbound,
)


@pytest.fixture(autouse=True)
def clean_outbound(tmp_path, monkeypatch):
    """Use a temp dir for outbound logs."""
    monkeypatch.setattr("stargate.outbound.OUTBOUND_DIR", tmp_path)
    yield tmp_path


class TestLogOutbound:
    def test_creates_file(self, clean_outbound):
        log_outbound("chat_123", "hello from buddy", "buddy")
        path = clean_outbound / "chat_123.jsonl"
        assert path.exists()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["source"] == "buddy"
        assert entry["text"] == "hello from buddy"
        assert "ts" in entry

    def test_appends_multiple(self, clean_outbound):
        log_outbound("key1", "msg1", "feathers")
        log_outbound("key1", "msg2", "feathers")
        log_outbound("key1", "msg3", "buddy")
        lines = (clean_outbound / "key1.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[2])["source"] == "buddy"

    def test_separate_session_keys(self, clean_outbound):
        log_outbound("key_a", "msg a", "buddy")
        log_outbound("key_b", "msg b", "feathers")
        assert (clean_outbound / "key_a.jsonl").exists()
        assert (clean_outbound / "key_b.jsonl").exists()
        a_lines = (clean_outbound / "key_a.jsonl").read_text().strip().splitlines()
        b_lines = (clean_outbound / "key_b.jsonl").read_text().strip().splitlines()
        assert len(a_lines) == 1
        assert len(b_lines) == 1

    def test_prunes_to_max_entries(self, clean_outbound):
        for i in range(MAX_ENTRIES + 5):
            log_outbound("pruned", f"msg{i}", "banana")
        lines = (clean_outbound / "pruned.jsonl").read_text().strip().splitlines()
        assert len(lines) == MAX_ENTRIES
        # Oldest messages should be pruned, newest kept
        last = json.loads(lines[-1])
        assert last["text"] == f"msg{MAX_ENTRIES + 4}"


class TestGetRecentOutbound:
    def test_empty_when_no_file(self, clean_outbound):
        assert get_recent_outbound("nonexistent") == []

    def test_returns_recent_entries(self, clean_outbound):
        log_outbound("recent", "msg1", "buddy")
        log_outbound("recent", "msg2", "feathers")
        results = get_recent_outbound("recent")
        assert len(results) == 2
        assert results[0]["text"] == "msg1"
        assert results[1]["text"] == "msg2"

    def test_filters_by_max_age(self, clean_outbound):
        # Write an old entry directly
        path = clean_outbound / "aged.jsonl"
        old_entry = json.dumps({"ts": time.time() - 100000, "source": "old", "text": "ancient"})
        new_entry = json.dumps({"ts": time.time(), "source": "new", "text": "fresh"})
        path.write_text(old_entry + "\n" + new_entry + "\n")

        results = get_recent_outbound("aged", max_age=3600.0)
        assert len(results) == 1
        assert results[0]["text"] == "fresh"

    def test_returns_all_within_default_age(self, clean_outbound):
        log_outbound("default", "msg1", "a")
        log_outbound("default", "msg2", "b")
        results = get_recent_outbound("default")
        assert len(results) == 2

    def test_handles_corrupt_lines(self, clean_outbound):
        path = clean_outbound / "corrupt.jsonl"
        good = json.dumps({"ts": time.time(), "source": "ok", "text": "fine"})
        path.write_text("not json\n" + good + "\n{broken\n")
        results = get_recent_outbound("corrupt")
        assert len(results) == 1
        assert results[0]["text"] == "fine"

    def test_excludes_claude_response_entries(self, clean_outbound):
        """get_recent_outbound must not surface claude-response audit
        entries — they'd pollute Claude's context and blow up the
        consumer that reads entry['text']."""
        from stargate.outbound import log_outbound_response

        log_outbound("mixed", "from agent", "buddy")
        log_outbound_response("mixed", 0, 1, "raw", "md", "MarkdownV2", "ok")
        results = get_recent_outbound("mixed")
        assert len(results) == 1
        assert results[0]["source"] == "buddy"


class TestLogOutboundResponse:
    """log_outbound_response audit-log API for Claude→Telegram sends (CTB-80f)."""

    def test_records_success_fields(self, clean_outbound):
        from stargate.outbound import log_outbound_response

        log_outbound_response(
            session_key="chat_1",
            chunk_index=0,
            chunk_total=1,
            raw="hello **bold**",
            md="hello *bold*",
            parse_mode="MarkdownV2",
            status="ok",
        )
        lines = (clean_outbound / "chat_1.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["source"] == "claude-response"
        assert entry["session_key"] == "chat_1"
        assert entry["chunk_index"] == 0
        assert entry["chunk_total"] == 1
        assert entry["raw_text"] == "hello **bold**"
        assert entry["md_text"] == "hello *bold*"
        assert entry["parse_mode"] == "MarkdownV2"
        assert entry["http_status"] == "ok"
        assert "ts" in entry

    def test_records_failure_with_exception_name(self, clean_outbound):
        from stargate.outbound import log_outbound_response

        log_outbound_response(
            session_key="chat_2",
            chunk_index=2,
            chunk_total=3,
            raw="chunk body",
            md=None,
            parse_mode="plain",
            status="BadRequest",
        )
        lines = (clean_outbound / "chat_2.jsonl").read_text().strip().splitlines()
        entry = json.loads(lines[0])
        assert entry["parse_mode"] == "plain"
        assert entry["md_text"] is None
        assert entry["http_status"] == "BadRequest"
        assert entry["chunk_index"] == 2
        assert entry["chunk_total"] == 3

    def test_separate_cap_for_response_entries(self, clean_outbound):
        """Response entries have their own higher cap and don't evict
        agent notifications."""
        from stargate.outbound import MAX_RESPONSE_ENTRIES, log_outbound_response

        log_outbound("budget", "agent msg", "buddy")
        for i in range(MAX_RESPONSE_ENTRIES + 5):
            log_outbound_response("budget", i, 1, f"r{i}", None, "plain", "ok")
        lines = (clean_outbound / "budget.jsonl").read_text().strip().splitlines()
        entries = [json.loads(line) for line in lines]
        notifications = [e for e in entries if e.get("source") != "claude-response"]
        responses = [e for e in entries if e.get("source") == "claude-response"]
        # Agent notification survives the response flood.
        assert len(notifications) == 1
        assert notifications[0]["text"] == "agent msg"
        # Response entries capped at MAX_RESPONSE_ENTRIES, newest kept.
        assert len(responses) == MAX_RESPONSE_ENTRIES
        assert responses[-1]["raw_text"] == f"r{MAX_RESPONSE_ENTRIES + 4}"
