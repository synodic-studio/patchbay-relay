"""Log filters to tame noisy repeated messages.

Primary use: Telegram 409 Conflict storms (CTB-72m) spam the log at several
records per second during restart overlap. We log the first occurrence, then
aggregate: every AGGREGATE_INTERVAL seconds we emit a single summary line
with the count, and swallow the intermediate records.
"""

from __future__ import annotations

import logging
import time

from .config import logger

# Aggregation window for noisy messages.
AGGREGATE_INTERVAL = 60.0  # seconds

# Substrings that mark 409 Conflict storm lines from python-telegram-bot.
CONFLICT_MARKERS = (
    "terminated by other getUpdates request",
    "telegram.error.Conflict",
    "Conflict: terminated by other",
)


class ConflictAggregator(logging.Filter):
    """Swallow repeated 409 Conflict log records; emit a summary periodically.

    Attaches to the root logger so it catches python-telegram-bot's
    `telegram.ext.Updater` and `httpx` emissions equally.

    Beyond rate-limiting the log spam, the aggregator also keeps a sliding
    window of recent conflict timestamps so a self-heal watcher (see
    bridge._conflict_storm_watcher) can detect a sustained storm and
    trigger the stale-poller repair (CTB-die).
    """

    # Drop entries older than this from the sliding window.
    RECENT_WINDOW_SEC = 60.0

    def __init__(self) -> None:
        super().__init__()
        self._count = 0
        self._first_ts: float | None = None
        self._last_summary_ts = 0.0
        self._recent_ts: list[float] = []

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if not any(m in msg for m in CONFLICT_MARKERS):
            return True  # pass-through non-matching records

        now = time.time()
        self._recent_ts.append(now)
        # Keep the sliding window pruned cheaply on every hit.
        cutoff = now - self.RECENT_WINDOW_SEC
        self._recent_ts = [ts for ts in self._recent_ts if ts >= cutoff]

        if self._count == 0:
            # First occurrence in this window — let it through, start counting.
            self._count = 1
            self._first_ts = now
            self._last_summary_ts = now
            return True

        self._count += 1
        # Every AGGREGATE_INTERVAL, emit a summary via a fresh logger call
        # (doesn't re-enter this filter because it uses a different logger).
        if now - self._last_summary_ts >= AGGREGATE_INTERVAL:
            elapsed = now - (self._first_ts or now)
            logger.warning(
                "Telegram 409 Conflict aggregated: %d occurrences over %.0fs "
                "— another poller is still active (CTB-72m).",
                self._count,
                elapsed,
            )
            self._last_summary_ts = now
        return False  # suppress this individual record

    def recent_count(self, window_sec: float | None = None) -> int:
        """Return the number of conflicts seen in the last `window_sec`
        seconds (default: RECENT_WINDOW_SEC). Cheap; used by the storm
        watcher to decide whether to dispatch a repair."""
        if window_sec is None:
            window_sec = self.RECENT_WINDOW_SEC
        cutoff = time.time() - window_sec
        return sum(1 for ts in self._recent_ts if ts >= cutoff)

    def reset_recent(self) -> None:
        """Clear the sliding window. The storm watcher calls this after a
        successful self-heal so a subsequent storm is detected fresh
        rather than triggering immediately on the residue."""
        self._recent_ts.clear()


def install_filters() -> ConflictAggregator:
    """Install log filters on the root logger. Returns the aggregator for
    introspection (e.g. the self-healer can read `.count` to detect a storm).
    """
    agg = ConflictAggregator()
    # Attach to the root so every module's emissions are inspected.
    logging.getLogger().addFilter(agg)
    # Also attach directly to known noisy telegram loggers to be safe across
    # python-telegram-bot versions.
    for name in ("telegram", "telegram.ext", "telegram.ext.Updater", "httpx"):
        logging.getLogger(name).addFilter(agg)
    return agg
