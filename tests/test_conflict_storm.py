"""Tests for the 409 Conflict storm detector and the runtime self-heal trigger.

Covers:
  - ConflictAggregator.recent_count sliding-window correctness
  - ConflictAggregator.reset_recent clears the window
  - bridge._conflict_storm_watcher fires dispatch_repair when threshold is
    crossed, respects the cooldown, and does nothing below threshold.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

import bridge
from stargate.log_filters import ConflictAggregator
from stargate.self_heal import RepairResult


def _conflict_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="telegram",
        level=logging.ERROR,
        pathname=__file__,
        lineno=0,
        msg="telegram.error.Conflict: terminated by other getUpdates request",
        args=(),
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# Aggregator sliding-window
# ---------------------------------------------------------------------------


class TestAggregatorWindow:
    def test_recent_count_starts_zero(self):
        agg = ConflictAggregator()
        assert agg.recent_count() == 0

    def test_records_each_conflict(self):
        agg = ConflictAggregator()
        for _ in range(5):
            agg.filter(_conflict_record())
        assert agg.recent_count() == 5

    def test_window_drops_old_entries(self):
        agg = ConflictAggregator()
        # Inject timestamps directly to skip the wall-clock dependency.
        now = time.time()
        agg._recent_ts = [now - 90, now - 70, now - 30, now - 5]
        # Default window is 60s; only the last two qualify.
        assert agg.recent_count() == 2

    def test_custom_window(self):
        agg = ConflictAggregator()
        now = time.time()
        agg._recent_ts = [now - 50, now - 20, now - 5]
        assert agg.recent_count(window_sec=10) == 1
        assert agg.recent_count(window_sec=30) == 2
        assert agg.recent_count(window_sec=120) == 3

    def test_reset_recent_clears_window(self):
        agg = ConflictAggregator()
        for _ in range(3):
            agg.filter(_conflict_record())
        assert agg.recent_count() > 0
        agg.reset_recent()
        assert agg.recent_count() == 0

    def test_non_conflict_records_pass_through(self):
        agg = ConflictAggregator()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname=__file__, lineno=0,
            msg="something else entirely", args=(), exc_info=None,
        )
        assert agg.filter(rec) is True
        assert agg.recent_count() == 0


# ---------------------------------------------------------------------------
# Storm watcher: dispatches repair when threshold crossed
# ---------------------------------------------------------------------------


class TestStormWatcher:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setattr(bridge, "CONFLICT_STORM_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(bridge, "CONFLICT_STORM_THRESHOLD", 5)
        monkeypatch.setattr(bridge, "CONFLICT_STORM_COOLDOWN", 60)

    @pytest.mark.asyncio
    async def test_no_dispatch_when_below_threshold(self, monkeypatch):
        agg = ConflictAggregator()
        agg._recent_ts = [time.time()] * 3  # below threshold of 5
        monkeypatch.setattr(bridge, "_conflict_aggregator", agg)

        with patch("stargate.self_heal.dispatch_repair") as mock_dispatch:
            task = asyncio.create_task(bridge._conflict_storm_watcher())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_when_threshold_crossed(self, monkeypatch):
        agg = ConflictAggregator()
        agg._recent_ts = [time.time()] * 10  # well above threshold
        monkeypatch.setattr(bridge, "_conflict_aggregator", agg)

        mock_dispatch = MagicMock(
            return_value=RepairResult(fixed=True, kind="stale_telegram_poller", actions=["sent SIGTERM"])
        )
        monkeypatch.setattr("stargate.self_heal.dispatch_repair", mock_dispatch)

        task = asyncio.create_task(bridge._conflict_storm_watcher())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_dispatch.assert_called_once()
        call_kw = mock_dispatch.call_args.args
        assert call_kw[0] == "stale_telegram_poller"
        assert call_kw[1]["conflict_count"] == 10
        # On successful repair, the window was reset
        assert agg.recent_count() == 0

    @pytest.mark.asyncio
    async def test_cooldown_prevents_re_fire(self, monkeypatch):
        agg = ConflictAggregator()
        agg._recent_ts = [time.time()] * 10
        monkeypatch.setattr(bridge, "_conflict_aggregator", agg)

        # No reset on this dispatch — the storm continues, but cooldown
        # should hold us back from re-firing in the same poll cycle.
        mock_dispatch = MagicMock(
            return_value=RepairResult(fixed=False, kind="stale_telegram_poller", actions=[])
        )
        monkeypatch.setattr("stargate.self_heal.dispatch_repair", mock_dispatch)

        task = asyncio.create_task(bridge._conflict_storm_watcher())
        await asyncio.sleep(0.1)  # multiple poll cycles
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Even though the watcher polled multiple times, it fires once.
        assert mock_dispatch.call_count == 1

    @pytest.mark.asyncio
    async def test_no_aggregator_no_dispatch(self, monkeypatch):
        """If install_filters wasn't called yet (e.g. early startup), the
        watcher should be a no-op rather than crash."""
        monkeypatch.setattr(bridge, "_conflict_aggregator", None)

        with patch("stargate.self_heal.dispatch_repair") as mock_dispatch:
            task = asyncio.create_task(bridge._conflict_storm_watcher())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_dispatch.assert_not_called()
