"""Test that log_activity writes valid JSON-lines to activity.jsonl."""

import json
import time

from unittest.mock import patch

from stargate.activity import log_activity


def test_log_activity_writes_valid_json_line(tmp_path):
    log_file = tmp_path / "activity.jsonl"

    with patch("stargate.activity.ACTIVITY_LOG", log_file):
        log_activity("test_event", user="alice", chat_id=42)

    lines = log_file.read_text().splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["event"] == "test_event"
    assert entry["user"] == "alice"
    assert entry["chat_id"] == 42
    assert isinstance(entry["ts"], float)


def test_log_activity_appends_multiple_lines(tmp_path):
    log_file = tmp_path / "activity.jsonl"

    with patch("stargate.activity.ACTIVITY_LOG", log_file):
        log_activity("first")
        log_activity("second", extra="data")

    lines = log_file.read_text().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["event"] == "first"
    assert second["event"] == "second"
    assert second["extra"] == "data"


def test_log_activity_each_line_is_valid_json(tmp_path):
    log_file = tmp_path / "activity.jsonl"

    with patch("stargate.activity.ACTIVITY_LOG", log_file):
        for i in range(5):
            log_activity("batch", index=i)

    lines = log_file.read_text().splitlines()
    assert len(lines) == 5

    for i, line in enumerate(lines):
        entry = json.loads(line)
        assert entry["event"] == "batch"
        assert entry["index"] == i


def test_log_activity_ts_is_recent(tmp_path):
    log_file = tmp_path / "activity.jsonl"
    before = time.time()

    with patch("stargate.activity.ACTIVITY_LOG", log_file):
        log_activity("timing")

    after = time.time()
    entry = json.loads(log_file.read_text().strip())
    assert before <= entry["ts"] <= after


def test_log_activity_handles_os_error(tmp_path):
    """OSError during write should not raise — just log debug."""
    bad_path = tmp_path / "no-such-dir" / "activity.jsonl"

    with patch("stargate.activity.ACTIVITY_LOG", bad_path):
        log_activity("should_not_crash")  # No exception raised
