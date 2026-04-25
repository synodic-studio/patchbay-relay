"""Tests for stargate.self_heal — repair dispatcher + built-in handlers."""

from __future__ import annotations

import json
import signal
from pathlib import Path
from unittest.mock import patch

import pytest

import stargate.self_heal as sh
from stargate.self_heal import (
    OOM_RETRY_MAX_TURNS,
    OOM_RETRY_PROMPT_TRIM,
    RepairResult,
    dispatch_repair,
    list_kinds,
    register_handler,
    reset_handlers,
)


@pytest.fixture
def isolated_handlers():
    """Snapshot the registry, restore after the test."""
    snapshot = dict(sh._HANDLERS)
    yield
    sh._HANDLERS.clear()
    sh._HANDLERS.update(snapshot)


@pytest.fixture
def activity_log_to_tmp(tmp_path, monkeypatch):
    """Redirect activity.jsonl writes into tmp_path so we can assert on them."""
    log_path = tmp_path / "activity.jsonl"
    monkeypatch.setattr("stargate.activity.ACTIVITY_LOG", log_path)
    return log_path


def _read_activity(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Registry / dispatcher behavior
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_built_in_kinds_registered(self):
        kinds = list_kinds()
        assert "corrupt_session_json" in kinds
        assert "stale_telegram_poller" in kinds
        assert "claude_oom_137" in kinds

    def test_unknown_kind_returns_unfixed(self, activity_log_to_tmp):
        result = dispatch_repair("does_not_exist", {})
        assert result.fixed is False
        assert result.kind == "does_not_exist"
        assert "no handler" in (result.error or "")

    def test_unknown_kind_logs_activity(self, activity_log_to_tmp):
        dispatch_repair("does_not_exist", {})
        events = _read_activity(activity_log_to_tmp)
        assert len(events) == 1
        assert events[0]["event"] == "self_heal"
        assert events[0]["fixed"] is False
        assert events[0]["kind"] == "does_not_exist"

    def test_handler_exception_caught(self, isolated_handlers, activity_log_to_tmp):
        @register_handler("explodes")
        def _bad(ctx):
            raise RuntimeError("boom")

        result = dispatch_repair("explodes", {})
        assert result.fixed is False
        assert result.error == "boom"

        events = _read_activity(activity_log_to_tmp)
        assert events[0]["error"] == "boom"

    def test_register_handler_decorator(self, isolated_handlers):
        @register_handler("custom_kind")
        def _h(ctx):
            return RepairResult(fixed=True, kind="custom_kind", actions=["did the thing"])

        result = dispatch_repair("custom_kind", {})
        assert result.fixed is True
        assert result.actions == ["did the thing"]

    def test_reset_handlers_clears_registry(self, isolated_handlers):
        reset_handlers()
        assert list_kinds() == []

    def test_dispatch_logs_success_path(self, isolated_handlers, activity_log_to_tmp):
        @register_handler("smooth")
        def _h(ctx):
            return RepairResult(fixed=True, kind="smooth", actions=["ok"])

        dispatch_repair("smooth", {})
        events = _read_activity(activity_log_to_tmp)
        assert events[0]["fixed"] is True
        assert events[0]["actions"] == ["ok"]


# ---------------------------------------------------------------------------
# corrupt_session_json handler
# ---------------------------------------------------------------------------


class TestCorruptSessionHandler:
    def test_quarantines_existing_file(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{{")

        result = dispatch_repair(
            "corrupt_session_json", {"path": bad, "reason": "test corruption"}
        )

        assert result.fixed is True
        assert not bad.exists()
        # quarantine_file moves to .quarantine sibling dir
        assert (tmp_path / ".quarantine").is_dir()
        moved = list((tmp_path / ".quarantine").iterdir())
        assert len(moved) == 1
        assert moved[0].name.startswith("bad.json.")

    def test_missing_file_treated_as_already_fixed(self, tmp_path):
        result = dispatch_repair(
            "corrupt_session_json", {"path": tmp_path / "never_existed.json"}
        )
        assert result.fixed is True
        assert "already gone" in result.actions[0]

    def test_missing_path_returns_unfixed(self):
        result = dispatch_repair("corrupt_session_json", {})
        assert result.fixed is False
        assert "missing or invalid" in (result.error or "")

    def test_path_as_string_accepted(self, tmp_path):
        bad = tmp_path / "stringpath.json"
        bad.write_text("garbage")
        result = dispatch_repair(
            "corrupt_session_json", {"path": str(bad), "reason": "x"}
        )
        assert result.fixed is True


# ---------------------------------------------------------------------------
# stale_telegram_poller handler
# ---------------------------------------------------------------------------


class TestStaleTelegramPollerHandler:
    def test_no_other_bridge_returns_unfixed(self, activity_log_to_tmp):
        with patch("stargate.singleton.signal_other_bridge", return_value=None):
            result = dispatch_repair("stale_telegram_poller", {})
        assert result.fixed is False
        assert "no other bridge PID" in result.actions[0]

    def test_signals_other_bridge_when_pid_found(self, activity_log_to_tmp):
        with patch("stargate.singleton.signal_other_bridge", return_value=12345) as m:
            result = dispatch_repair("stale_telegram_poller", {})
        assert result.fixed is True
        assert "12345" in result.actions[0]
        m.assert_called_once_with(signal.SIGTERM)

    def test_custom_signal_passed_through(self, activity_log_to_tmp):
        with patch("stargate.singleton.signal_other_bridge", return_value=99) as m:
            dispatch_repair("stale_telegram_poller", {"signal": signal.SIGKILL})
        m.assert_called_once_with(signal.SIGKILL)


# ---------------------------------------------------------------------------
# claude_oom_137 handler
# ---------------------------------------------------------------------------


class TestClaudeOOMHandler:
    def test_returncode_137_is_fixed(self):
        result = dispatch_repair(
            "claude_oom_137", {"session_key": "k1", "returncode": 137}
        )
        assert result.fixed is True
        assert "k1" in result.actions[0]
        assert str(OOM_RETRY_MAX_TURNS) in result.actions[0]
        assert str(OOM_RETRY_PROMPT_TRIM) in result.actions[0]

    def test_returncode_neg9_is_fixed(self):
        """SIGKILL via OOM-killer surfaces as -9 in subprocess."""
        result = dispatch_repair(
            "claude_oom_137", {"session_key": "k1", "returncode": -9}
        )
        assert result.fixed is True

    def test_unrelated_returncode_returns_unfixed(self):
        result = dispatch_repair(
            "claude_oom_137", {"session_key": "k1", "returncode": 1}
        )
        assert result.fixed is False
        assert "not OOM-shaped" in (result.error or "")

    def test_missing_returncode_returns_unfixed(self):
        result = dispatch_repair("claude_oom_137", {"session_key": "k1"})
        assert result.fixed is False
