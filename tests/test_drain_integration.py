"""End-to-end integration test for the drain/debounce path.

Drives `bridge.handle_message` through both the claim-the-lane and
queue-and-drain branches with a real second handler invocation (not a
manual queue.append). Synchronization is via `threading.Event`, not
`asyncio.sleep(0)` — the latter was the source of a historical
`test_queued_messages_drained_after_processing` flake.

The test (a) starts handler A via a task, (b) sends handler B a second
message via the real Telegram handler API, (c) asserts both replies
land and in order.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge


@pytest.fixture(autouse=True)
def _clean_state():
    bridge._sessions.clear()
    yield
    bridge._sessions.clear()


def _make_update(chat_id=1, thread_id=None, text="hello", user_id=42):
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
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_two_handlers_real_second_call_both_responses_land_in_order():
    """Handler A is mid-flight. Handler B fires through the *real* handler
    (not a manual queue.append) and is queued. When A finishes, B drains
    and produces a second send_message. Both responses land, in order."""
    ctx_a = _make_context()
    ctx_b = _make_context()

    run_claude_calls: list[str] = []
    first_entered = threading.Event()
    release_first = threading.Event()
    second_handler_done = threading.Event()

    def fake_run_claude(message, session_key, model=None):
        run_claude_calls.append(message)
        if len(run_claude_calls) == 1:
            first_entered.set()
            release_first.wait(timeout=5)  # block until handler B has queued
        return f"RESP::{message[:20]}"

    with (
        patch.object(bridge, "run_claude", side_effect=fake_run_claude),
        patch.object(bridge, "save_pending", return_value="pending-x"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
    ):
        # Handler A
        update_a = _make_update(text="first message")
        task_a = asyncio.create_task(bridge.handle_message(update_a, ctx_a))

        # Wait until handler A is actually inside fake_run_claude
        while not first_entered.is_set():
            await asyncio.sleep(0.01)

        # Handler B fires through the *real* handler API while A is blocked.
        # The flow: claim-or-queue sees processing=True → status="queued".
        update_b = _make_update(text="second message")
        await bridge.handle_message(update_b, ctx_b)
        second_handler_done.set()

        # Verify B was queued, not executed. A is still blocked.
        assert len(run_claude_calls) == 1
        update_b.message.reply_text.assert_called_once()
        queued_reply = update_b.message.reply_text.call_args[0][0]
        assert "Queued (1)" in queued_reply

        # Release A. Drain loop in handler A picks up B's message.
        release_first.set()
        await task_a

    # Both invocations landed
    assert len(run_claude_calls) == 2
    assert run_claude_calls[0] == "first message"
    assert run_claude_calls[1] == "second message"

    # Handler A's bot got both responses (the drain loop sends from A's context).
    assert ctx_a.bot.send_message.call_count == 2
    sent_texts = [c.kwargs.get("text") or c.args[0] for c in ctx_a.bot.send_message.call_args_list]
    # Order matters: A's response arrives before B's drained response.
    assert "first message" in sent_texts[0]
    assert "second message" in sent_texts[1]

    # Handler B's only user-visible side effect was the "Queued" reply,
    # not a send_message — its reply was drained from A's context.
    ctx_b.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_three_handlers_combined_into_one_drain_batch():
    """Handlers B and C both fire while A is blocked. They get combined
    into a single follow-up batch on drain — exercising the multi-message
    drain path without `asyncio.sleep(0)` synchronization."""
    ctx = _make_context()
    run_claude_calls: list[str] = []
    first_entered = threading.Event()
    release_first = threading.Event()

    def fake_run_claude(message, session_key, model=None):
        run_claude_calls.append(message)
        if len(run_claude_calls) == 1:
            first_entered.set()
            release_first.wait(timeout=5)
        return f"RESP::{message[:30]}"

    with (
        patch.object(bridge, "run_claude", side_effect=fake_run_claude),
        patch.object(bridge, "save_pending", return_value="pending-x"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
    ):
        update_a = _make_update(text="msg-A")
        task_a = asyncio.create_task(bridge.handle_message(update_a, ctx))

        while not first_entered.is_set():
            await asyncio.sleep(0.01)

        # Two more handler invocations through the real API, both queue.
        await bridge.handle_message(_make_update(text="msg-B"), _make_context())
        await bridge.handle_message(_make_update(text="msg-C"), _make_context())

        assert len(run_claude_calls) == 1  # only A has run

        release_first.set()
        await task_a

    # A ran, then a *combined* batch ran for B+C (single drain pop).
    assert len(run_claude_calls) == 2
    assert run_claude_calls[0] == "msg-A"
    combined = run_claude_calls[1]
    assert "[Follow-up 1]" in combined
    assert "[Follow-up 2]" in combined
    assert "msg-B" in combined
    assert "msg-C" in combined


@pytest.mark.asyncio
async def test_no_lane_leak_after_drain_completes():
    """After drain finishes, the session's processing flag is cleared so
    a future message starts fresh."""
    ctx = _make_context()
    key = bridge._session_key(1, None)

    with (
        patch.object(bridge, "run_claude", return_value="ok"),
        patch.object(bridge, "save_pending", return_value="pending-x"),
        patch.object(bridge, "clear_pending"),
        patch.object(bridge, "keep_typing", new=AsyncMock()),
    ):
        await bridge.handle_message(_make_update(text="solo"), ctx)

    state = bridge._sessions.get(key)
    assert state is None or not state.processing
    assert state is None or not state.queue
