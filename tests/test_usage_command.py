"""Tests for /usage command helpers and the Prototype A rendering shape."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


class TestFormatTokens:
    def test_small(self):
        import bridge

        assert bridge._format_tokens(999) == "999"

    def test_thousands(self):
        import bridge

        assert bridge._format_tokens(12_500) == "12.5K"

    def test_millions(self):
        import bridge

        assert bridge._format_tokens(72_100_000) == "72.1M"

    def test_billions(self):
        """Added for weekly caps: 3_000_000_000 should render as 3.0B."""
        import bridge

        assert bridge._format_tokens(3_000_000_000) == "3.0B"
        assert bridge._format_tokens(437_200_000) == "437.2M"


class TestBar:
    def test_zero_percent_is_all_empty(self):
        import bridge

        bar = bridge._bar(0.0, width=10)
        assert bar == "░" * 10

    def test_hundred_percent_is_all_filled(self):
        import bridge

        bar = bridge._bar(100.0, width=10)
        assert bar == "█" * 10

    def test_fifty_percent_half_filled(self):
        import bridge

        bar = bridge._bar(50.0, width=10)
        assert bar == "█" * 5 + "░" * 5

    def test_clamps_over_100(self):
        import bridge

        assert bridge._bar(150.0, width=8) == "█" * 8

    def test_clamps_under_0(self):
        import bridge

        assert bridge._bar(-10.0, width=8) == "░" * 8

    def test_default_width_12(self):
        import bridge

        assert len(bridge._bar(33.0)) == 12


class TestBlockTimePercent:
    def test_parses_iso_and_returns_percent(self):
        """Midway through a 5h block should be ~50% elapsed."""
        from datetime import datetime, timedelta, timezone

        import bridge

        # Construct a block whose midpoint is "now".
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=2, minutes=30)).isoformat().replace("+00:00", "Z")
        end = (now + timedelta(hours=2, minutes=30)).isoformat().replace("+00:00", "Z")
        pct = bridge._block_time_percent(start, end)
        assert pct is not None
        assert 45 < pct < 55

    def test_invalid_iso_returns_none(self):
        import bridge

        assert bridge._block_time_percent("not-iso", "also-not-iso") is None

    def test_zero_span_returns_none(self):
        """Guard against div/0 if start == end."""
        import bridge

        stamp = "2026-04-22T18:00:00Z"
        assert bridge._block_time_percent(stamp, stamp) is None


# ---------------------------------------------------------------------------
# cmd_usage integration — happy path with mocked ccusage
# ---------------------------------------------------------------------------


def _proc(stdout: str):
    """Build a fake subprocess.CompletedProcess-ish with stdout."""
    p = MagicMock()
    p.stdout = stdout
    p.returncode = 0
    return p


class TestCmdUsageDispatch:
    """cmd_usage dispatches to the topic's harness; pi reports session cost."""

    @pytest.mark.asyncio
    async def test_pi_shows_session_cost(self, monkeypatch):
        import bridge
        from patchbay.harness import SessionUsage

        class FakeHarness:
            capabilities = None

            async def get_usage(self, req):
                return SessionUsage(
                    cost_usd=0.0123, input_tokens=100, output_tokens=20,
                    total_tokens=120, model="small",
                )

        req = MagicMock()
        req.resume_session_id = "sid"
        monkeypatch.setattr(
            "patchbay.commands.inquiry.resolve_harness_for_inquiry",
            lambda key: ("pi", FakeHarness(), req),
        )

        update = MagicMock()
        update.effective_chat.id = 123
        update.message.message_thread_id = 456
        update.message.reply_text = AsyncMock()
        await bridge.cmd_usage(update, MagicMock())
        sent = update.message.reply_text.call_args.args[0]
        assert "Session usage (pi)" in sent
        assert "$0.0123" in sent
        assert "120" in sent  # total tokens rendered


class TestCmdUsageRendering:
    """Verify the parked ccusage path produces the Prototype A shape."""

    @pytest.mark.asyncio
    async def test_renders_both_bars_and_weekly_cap(self, monkeypatch):
        import bridge

        monkeypatch.setattr(bridge, "USAGE_WEEKLY_TOKEN_CAP", 3_000_000_000)

        blocks_json = json.dumps(
            {
                "blocks": [
                    {
                        "startTime": "2026-04-22T18:00:00.000Z",
                        "endTime": "2026-04-22T23:00:00.000Z",
                        "totalTokens": 74_000_000,
                        "tokenLimitStatus": {
                            "limit": 414_000_000,
                            "percentUsed": 17.87,
                            "status": "ok",
                        },
                        "projection": {"remainingMinutes": 125},
                    }
                ]
            }
        )
        weekly_json = json.dumps(
            {
                "weekly": [
                    {
                        "week": "2026-04-20",
                        "totalTokens": 439_900_000,
                    }
                ]
            }
        )

        async def fake_gather(*coros):
            # Consume the coroutines so Python doesn't warn, return fake results.
            for c in coros:
                c.close()
            return _proc(blocks_json), _proc(weekly_json)

        monkeypatch.setattr(bridge.asyncio, "gather", fake_gather)

        update = MagicMock()
        update.message.reply_text = AsyncMock()
        await bridge._ccusage_report(update)
        sent = update.message.reply_text.call_args.args[0]

        # Must contain both sections.
        assert "5h block:" in sent
        assert "Week (cap 3.0B est):" in sent

        # Both token and time bars present in each section.
        assert sent.count("tokens ") >= 2
        assert sent.count("time    ") >= 2

        # Bar glyphs present.
        assert "█" in sent or "░" in sent

        # 5h block percent came from ccusage's tokenLimitStatus.
        assert "17.9%" in sent or "17.8%" in sent

        # Weekly percent computed from cap: 439.9M / 3B ≈ 14.66%.
        assert "14.6%" in sent or "14.7%" in sent

    @pytest.mark.asyncio
    async def test_ccusage_missing_reports_error(self, monkeypatch):
        import bridge

        async def fake_gather(*coros):
            for c in coros:
                c.close()
            raise FileNotFoundError("ccusage")

        monkeypatch.setattr(bridge.asyncio, "gather", fake_gather)

        update = MagicMock()
        update.message.reply_text = AsyncMock()
        await bridge._ccusage_report(update)
        sent = update.message.reply_text.call_args.args[0]
        assert "usage check failed" in sent

    @pytest.mark.asyncio
    async def test_no_active_block_and_no_weekly_data(self, monkeypatch):
        import bridge

        async def fake_gather(*coros):
            for c in coros:
                c.close()
            return _proc(json.dumps({"blocks": []})), _proc(json.dumps({"weekly": []}))

        monkeypatch.setattr(bridge.asyncio, "gather", fake_gather)

        update = MagicMock()
        update.message.reply_text = AsyncMock()
        await bridge._ccusage_report(update)
        sent = update.message.reply_text.call_args.args[0]
        assert "5h block: (none)" in sent
        # Week line still renders with 0 tokens used.
        assert "Week (cap" in sent
