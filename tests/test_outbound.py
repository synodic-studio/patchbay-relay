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
