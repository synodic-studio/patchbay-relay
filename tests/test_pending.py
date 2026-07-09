"""Tests for pending message file write/clear lifecycle."""

import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory so we can import bridge
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge

# bridge.PENDING_DIR is read fresh in each test so conftest's autouse path
# isolation (which monkeypatches the attribute to a tmp dir) is picked up.
# Don't capture it as a module-level constant — that snapshots the real
# production path before any fixture runs.


class TestSavePending:
    def test_creates_json_file(self):
        pid = bridge.save_pending(123, 456, "hello", "123:456")
        path = bridge.PENDING_DIR / f"{pid}.json"
        assert path.exists()

    def test_file_contains_correct_fields(self):
        pid = bridge.save_pending(123, 456, "hello world", "123:456")
        data = json.loads((bridge.PENDING_DIR / f"{pid}.json").read_text())
        assert data["chat_id"] == 123
        assert data["thread_id"] == 456
        assert data["text"] == "hello world"
        assert data["session_key"] == "123:456"
        assert isinstance(data["timestamp"], float)

    def test_none_thread_id(self):
        pid = bridge.save_pending(123, None, "hi", "123:None")
        data = json.loads((bridge.PENDING_DIR / f"{pid}.json").read_text())
        assert data["thread_id"] is None

    def test_returns_unique_ids(self):
        ids = {bridge.save_pending(1, None, "m", "1:None") for _ in range(20)}
        assert len(ids) == 20


class TestClearPending:
    def test_removes_file(self):
        pid = bridge.save_pending(1, None, "text", "1:None")
        path = bridge.PENDING_DIR / f"{pid}.json"
        assert path.exists()
        bridge.clear_pending(pid)
        assert not path.exists()

    def test_no_error_on_missing_file(self):
        bridge.clear_pending("nonexistent_id_abc")  # should not raise


class TestReplayPending:
    @pytest.mark.asyncio
    async def test_skips_malformed_json(self):
        (bridge.PENDING_DIR / "bad.json").write_text("not json{{{")
        bot = AsyncMock()
        await bridge.replay_pending(bot)
        assert not (bridge.PENDING_DIR / "bad.json").exists()

    @pytest.mark.asyncio
    async def test_skips_missing_required_keys(self):
        (bridge.PENDING_DIR / "incomplete.json").write_text(json.dumps({"chat_id": 1, "text": "hi"}))
        bot = AsyncMock()
        await bridge.replay_pending(bot)
        assert not (bridge.PENDING_DIR / "incomplete.json").exists()

    @pytest.mark.asyncio
    async def test_skips_expired_messages(self):
        expired_data = {
            "chat_id": 1,
            "thread_id": None,
            "text": "old",
            "session_key": "1:None",
            "timestamp": time.time() - bridge.SESSION_EXPIRY - 100,
        }
        (bridge.PENDING_DIR / "expired.json").write_text(json.dumps(expired_data))
        bot = AsyncMock()
        await bridge.replay_pending(bot)
        assert not (bridge.PENDING_DIR / "expired.json").exists()

    @pytest.mark.asyncio
    async def test_replays_valid_message(self):
        pid = bridge.save_pending(99, 10, "replay me", "99:10")
        bot = AsyncMock()

        with patch.object(bridge, "run_claude", return_value="response text"):
            await bridge.replay_pending(bot)

        assert not (bridge.PENDING_DIR / f"{pid}.json").exists()
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args
        text_sent = call_kwargs.kwargs.get("text", call_kwargs.args[0] if call_kwargs.args else "")
        assert "Recovered after bridge restart" in text_sent

    @pytest.mark.asyncio
    async def test_noop_when_no_pending_files(self):
        bot = AsyncMock()
        await bridge.replay_pending(bot)
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_present_with_bumped_attempts_during_processing(self):
        """File stays during run_claude (so crashes don't lose it), but with
        attempts incremented to 1. After successful send, file is deleted."""
        pid = bridge.save_pending(1, None, "msg", "1_None")
        path = bridge.PENDING_DIR / f"{pid}.json"

        observed = []

        def fake_run_claude(text, key):
            data = json.loads(path.read_text())
            observed.append((path.exists(), data.get("attempts")))
            return "ok"

        bot = AsyncMock()
        with patch.object(bridge, "run_claude", side_effect=fake_run_claude):
            await bridge.replay_pending(bot)

        assert observed == [(True, 1)]
        assert not path.exists()  # deleted on successful delivery


class TestPendingSurvivesDeliveryDisruption:
    """Pin the SIGTERM-mid-send hardening: the pending file must remain on
    disk when delivery is interrupted, so the next bridge start can replay
    via replay_pending(). Three failure modes are covered:

      1. _send_response raises a regular Exception (network/Telegram error).
      2. The coroutine is cancelled mid-send (asyncio.CancelledError, what
         SIGTERM looks like inside the loop).
      3. Queue is full → the pending file we just saved gets cleaned up
         (the user was told the message was dropped; replay would surprise).

    A regression here would mean lost replies on bridge restart — exactly
    the failure mode that motivated this hardening.
    """

    @pytest.mark.asyncio
    async def test_pending_survives_send_exception(self):
        """If _send_response raises, the pending file must NOT be cleared."""
        chat_id, thread_id = 999, 5
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_chat_action = AsyncMock()

        async def boom(*_args, **_kwargs):
            raise RuntimeError("telegram unavailable")

        with (
            patch.object(bridge, "run_claude", return_value="some response"),
            patch.object(bridge, "_send_response", side_effect=boom),
            patch.object(bridge, "_notify_delivery_failure", new=AsyncMock()),
            patch.object(bridge, "keep_typing", new=AsyncMock()),
        ):
            await bridge._process_with_claude_turn(
                update,
                ctx,
                session_key="t1",
                chat_id=chat_id,
                thread_id=thread_id,
                prompt="hello",
                label="message",
                drop_message="full",
                queued_message="queued ({depth})",
            )

        # Exactly one pending file remains, with our prompt in it.
        files = list(bridge.PENDING_DIR.glob("*.json"))
        assert len(files) == 1, f"expected 1 pending file, got {[f.name for f in files]}"
        data = json.loads(files[0].read_text())
        assert data["text"] == "hello"
        assert data["chat_id"] == chat_id

    @pytest.mark.asyncio
    async def test_pending_survives_send_cancellation(self):
        """If the coroutine is cancelled (SIGTERM analogue), pending stays.

        CancelledError is a BaseException and skips ``except Exception``,
        but the ``finally`` block runs with delivered=False and leaves the
        file alone — that's the invariant under test.
        """
        import asyncio

        chat_id, thread_id = 1234, 7
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_chat_action = AsyncMock()

        send_started = asyncio.Event()

        async def hang_then_cancel(*_args, **_kwargs):
            send_started.set()
            await asyncio.sleep(60)  # will be cancelled before this returns

        with (
            patch.object(bridge, "run_claude", return_value="some response"),
            patch.object(bridge, "_send_response", side_effect=hang_then_cancel),
            patch.object(bridge, "_notify_delivery_failure", new=AsyncMock()),
            patch.object(bridge, "keep_typing", new=AsyncMock()),
        ):
            task = asyncio.create_task(
                bridge._process_with_claude_turn(
                    update,
                    ctx,
                    session_key="t2",
                    chat_id=chat_id,
                    thread_id=thread_id,
                    prompt="cancelled mid-send",
                    label="message",
                    drop_message="full",
                    queued_message="queued ({depth})",
                )
            )
            await send_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        files = list(bridge.PENDING_DIR.glob("*.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text())["text"] == "cancelled mid-send"

    @pytest.mark.asyncio
    async def test_pending_cleared_when_queue_is_full(self):
        """Full queue → the message is dropped. The pending file we saved
        on the way in must be cleared so it doesn't get replayed."""
        chat_id, thread_id = 5, 99
        session_key = "t3"
        # Pre-fill the queue past MAX_QUEUED_MESSAGES so the next message goes "full".
        state = bridge._get_session_state(session_key)
        state.processing = True
        state.queue = [
            bridge.QueuedMessage(text=f"q{i}", pending_id=f"pid{i}")
            for i in range(bridge.MAX_QUEUED_MESSAGES)
        ]

        update = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_chat_action = AsyncMock()

        await bridge._process_with_claude_turn(
            update,
            ctx,
            session_key=session_key,
            chat_id=chat_id,
            thread_id=thread_id,
            prompt="this gets dropped",
            label="message",
            drop_message="full",
            queued_message="queued ({depth})",
        )

        # No pending file left for the dropped message — we said we wouldn't
        # process it, replaying would be a lie.
        files = [
            f
            for f in bridge.PENDING_DIR.glob("*.json")
            if json.loads(f.read_text()).get("text") == "this gets dropped"
        ]
        assert files == []

    @pytest.mark.asyncio
    async def test_queued_message_writes_pending_file(self):
        """A message that gets queued (not claimed) must still have a pending
        file on disk so SIGTERM-during-debounce doesn't lose it."""
        chat_id, thread_id = 88, 1
        session_key = "t4"
        # Mark the session as already-processing so the next message queues
        # rather than claiming the lane.
        bridge._get_session_state(session_key).processing = True

        update = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = MagicMock()

        await bridge._process_with_claude_turn(
            update,
            ctx,
            session_key=session_key,
            chat_id=chat_id,
            thread_id=thread_id,
            prompt="follow-up while busy",
            label="message",
            drop_message="full",
            queued_message="queued ({depth})",
        )

        files = list(bridge.PENDING_DIR.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["text"] == "follow-up while busy"
        # And the queued item carries the same pending_id back to the drain path.
        queue = bridge._get_session_state(session_key).queue
        assert len(queue) == 1
        assert queue[0].text == "follow-up while busy"
        assert queue[0].pending_id == files[0].stem
