"""Tests for message debounce queuing in bridge.py.

Verifies that follow-up messages arriving while Claude is processing
get queued, batched, and drained correctly.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge


@pytest.fixture(autouse=True)
def _clean_bridge_state():
    """Reset bridge module-level debounce state between tests."""
    bridge._processing_sessions.clear()
    bridge._queued_messages.clear()
    bridge._session_start_times.clear()
    original = bridge.ALLOWED_USER_IDS
    bridge.ALLOWED_USER_IDS = set()  # disable allowlist for tests
    yield
    bridge._processing_sessions.clear()
    bridge._queued_messages.clear()
    bridge._session_start_times.clear()
    bridge.ALLOWED_USER_IDS = original


def _make_update(chat_id=1, thread_id=None, text="hello", user_id=42):
    """Build a minimal mock Update for handle_message."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.reply_text = AsyncMock()
    return update


def _make_context():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


# ── Queuing behavior ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_queued_when_session_processing():
    """A message arriving while session is processing should be queued."""
    key = bridge._session_key(1, None)
    bridge._processing_sessions.add(key)

    update = _make_update(text="follow-up")
    ctx = _make_context()

    with patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)):
        await bridge.handle_message(update, ctx)

    assert key in bridge._queued_messages
    assert bridge._queued_messages[key] == ["follow-up"]
    update.message.reply_text.assert_called_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "Queued (1)" in reply_text


@pytest.mark.asyncio
async def test_multiple_messages_queue_incrementally():
    """Multiple follow-ups should queue with increasing depth."""
    key = bridge._session_key(1, None)
    bridge._processing_sessions.add(key)

    ctx = _make_context()

    with patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)):
        for i in range(3):
            update = _make_update(text=f"msg-{i}")
            await bridge.handle_message(update, ctx)

    assert len(bridge._queued_messages[key]) == 3
    assert bridge._queued_messages[key] == ["msg-0", "msg-1", "msg-2"]


@pytest.mark.asyncio
async def test_queued_reply_shows_depth():
    """Each queued reply should show the correct queue depth."""
    key = bridge._session_key(1, None)
    bridge._processing_sessions.add(key)

    ctx = _make_context()
    updates = []

    with patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)):
        for i in range(3):
            u = _make_update(text=f"msg-{i}")
            updates.append(u)
            await bridge.handle_message(u, ctx)

    for i, u in enumerate(updates, start=1):
        reply = u.message.reply_text.call_args[0][0]
        assert f"Queued ({i})" in reply


# ── Drain behavior ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queued_messages_drained_after_processing():
    """After the initial response, queued messages should be processed."""
    key = bridge._session_key(1, None)
    ctx = _make_context()

    run_claude_calls = []

    def fake_run_claude(message, session_key):
        run_claude_calls.append(message)
        return f"response to: {message[:20]}"

    with (
        patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)),
        patch.object(bridge, "run_claude", side_effect=fake_run_claude),
        patch.object(bridge, "save_pending", return_value="pending-1"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
        patch.object(bridge, "_handoff_to_forge", return_value=False),
    ):
        first_update = _make_update(text="initial question")
        task = asyncio.create_task(bridge.handle_message(first_update, ctx))

        # Wait for the first message to enter processing
        await asyncio.sleep(0)

        # Simulate follow-up arriving while processing
        bridge._queued_messages.setdefault(key, []).append("follow-up 1")

        await task

    assert len(run_claude_calls) == 2
    assert run_claude_calls[0] == "initial question"
    assert run_claude_calls[1] == "follow-up 1"


@pytest.mark.asyncio
async def test_multiple_queued_messages_combined_with_separator():
    """Multiple queued messages should be combined with follow-up format."""
    key = bridge._session_key(1, None)
    ctx = _make_context()

    run_claude_calls = []

    def fake_run_claude(message, session_key):
        run_claude_calls.append(message)
        return "ok"

    with (
        patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)),
        patch.object(bridge, "run_claude", side_effect=fake_run_claude),
        patch.object(bridge, "save_pending", return_value="pending-1"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
        patch.object(bridge, "_handoff_to_forge", return_value=False),
    ):
        first_update = _make_update(text="initial")
        task = asyncio.create_task(bridge.handle_message(first_update, ctx))
        await asyncio.sleep(0)

        bridge._queued_messages.setdefault(key, []).extend(["second msg", "third msg"])

        await task

    assert len(run_claude_calls) == 2
    combined = run_claude_calls[1]
    assert "[Follow-up 1]" in combined
    assert "[Follow-up 2]" in combined
    assert "second msg" in combined
    assert "third msg" in combined
    assert "---" in combined


@pytest.mark.asyncio
async def test_single_queued_message_sent_without_follow_up_format():
    """A single queued message should be sent as-is, not wrapped in follow-up format."""
    key = bridge._session_key(1, None)
    ctx = _make_context()

    run_claude_calls = []

    def fake_run_claude(message, session_key):
        run_claude_calls.append(message)
        return "ok"

    with (
        patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)),
        patch.object(bridge, "run_claude", side_effect=fake_run_claude),
        patch.object(bridge, "save_pending", return_value="pending-1"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
        patch.object(bridge, "_handoff_to_forge", return_value=False),
    ):
        first_update = _make_update(text="initial")
        task = asyncio.create_task(bridge.handle_message(first_update, ctx))
        await asyncio.sleep(0)

        bridge._queued_messages.setdefault(key, []).append("just one follow-up")

        await task

    assert run_claude_calls[1] == "just one follow-up"
    assert "[Follow-up" not in run_claude_calls[1]


# ── Session cleanup ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_cleared_after_processing_completes():
    """Session should be removed from _processing_sessions after handle_message."""
    key = bridge._session_key(1, None)
    ctx = _make_context()

    with (
        patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)),
        patch.object(bridge, "run_claude", return_value="done"),
        patch.object(bridge, "save_pending", return_value="pending-1"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
        patch.object(bridge, "_handoff_to_forge", return_value=False),
    ):
        update = _make_update(text="test")
        await bridge.handle_message(update, ctx)

    assert key not in bridge._processing_sessions
    assert key not in bridge._session_start_times


@pytest.mark.asyncio
async def test_session_cleared_even_on_error():
    """Session should be cleaned up even if run_claude raises."""
    key = bridge._session_key(1, None)
    ctx = _make_context()

    with (
        patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)),
        patch.object(bridge, "run_claude", side_effect=RuntimeError("boom")),
        patch.object(bridge, "save_pending", return_value="pending-1"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
        patch.object(bridge, "_handoff_to_forge", return_value=False),
    ):
        update = _make_update(text="test")
        await bridge.handle_message(update, ctx)

    assert key not in bridge._processing_sessions


# ── Non-queued path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_not_queued_when_session_idle():
    """A message to an idle session should go straight to processing, not queue."""
    key = bridge._session_key(1, None)
    ctx = _make_context()

    with (
        patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)),
        patch.object(bridge, "run_claude", return_value="response"),
        patch.object(bridge, "save_pending", return_value="pending-1"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
        patch.object(bridge, "_handoff_to_forge", return_value=False),
    ):
        update = _make_update(text="normal message")
        await bridge.handle_message(update, ctx)

    assert key not in bridge._queued_messages
    ctx.bot.send_message.assert_called()


@pytest.mark.asyncio
async def test_forum_topic_sessions_queue_independently():
    """Messages in different forum topics should have independent queues."""
    key_a = bridge._session_key(1, 100)
    key_b = bridge._session_key(1, 200)

    bridge._processing_sessions.add(key_a)

    ctx = _make_context()

    with (
        patch.object(bridge, "_check_auth", new=AsyncMock(return_value=True)),
        patch.object(bridge, "run_claude", return_value="ok"),
        patch.object(bridge, "save_pending", return_value="p1"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
        patch.object(bridge, "_handoff_to_forge", return_value=False),
    ):
        update_a = _make_update(chat_id=1, thread_id=100, text="to A")
        await bridge.handle_message(update_a, ctx)

        update_b = _make_update(chat_id=1, thread_id=200, text="to B")
        await bridge.handle_message(update_b, ctx)

    assert key_a in bridge._queued_messages
    assert key_b not in bridge._queued_messages
