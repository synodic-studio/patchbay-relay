"""Tests for SessionState and the _claim_or_queue / _drain_next helpers.

Covers a debounce race where two messages arriving in the same event-loop
tick must produce exactly one "claimed" and the rest "queued" — without
the asyncio.Lock, both could pass the `if state.processing` check and
stomp on each other.
"""

import asyncio

import pytest

import bridge


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(bridge, "_sessions", {})


@pytest.mark.asyncio
async def test_first_caller_claims():
    state = bridge._get_session_state("k")
    status, depth = await bridge._claim_or_queue(state, "hi")
    assert status == "claimed"
    assert depth is None
    assert state.processing is True
    assert state.started_at is not None


@pytest.mark.asyncio
async def test_second_caller_queues():
    state = bridge._get_session_state("k")
    await bridge._claim_or_queue(state, "first")  # claimed
    status, depth = await bridge._claim_or_queue(state, "second")
    assert status == "queued"
    assert depth == 1
    assert state.queue == ["second"]


@pytest.mark.asyncio
async def test_queue_full_returns_full(monkeypatch):
    monkeypatch.setattr(bridge, "MAX_QUEUED_MESSAGES", 2)
    state = bridge._get_session_state("k")
    await bridge._claim_or_queue(state, "first")  # claimed
    await bridge._claim_or_queue(state, "queued1")
    await bridge._claim_or_queue(state, "queued2")
    status, depth = await bridge._claim_or_queue(state, "overflow")
    assert status == "full"
    assert depth is None
    assert state.queue == ["queued1", "queued2"]


@pytest.mark.asyncio
async def test_concurrent_claims_serialize():
    """The race fix: gather many _claim_or_queue calls; only one wins."""
    state = bridge._get_session_state("k")
    results = await asyncio.gather(*[bridge._claim_or_queue(state, f"m{i}") for i in range(10)])
    statuses = [r[0] for r in results]
    assert statuses.count("claimed") == 1
    assert statuses.count("queued") == 9
    # All 9 queued messages got into the queue
    assert len(state.queue) == 9


@pytest.mark.asyncio
async def test_drain_next_returns_batch_and_clears_queue():
    state = bridge._get_session_state("k")
    state.processing = True
    state.queue = ["a", "b", "c"]
    batch = await bridge._drain_next(state)
    assert batch == ["a", "b", "c"]
    assert state.queue == []
    # Still processing — drain only releases when queue is empty AT call time
    assert state.processing is True


@pytest.mark.asyncio
async def test_drain_next_releases_when_empty():
    state = bridge._get_session_state("k")
    state.processing = True
    state.started_at = 1000.0
    batch = await bridge._drain_next(state)
    assert batch is None
    assert state.processing is False
    assert state.started_at is None


@pytest.mark.asyncio
async def test_release_processing_clears_flag():
    state = bridge._get_session_state("k")
    state.processing = True
    state.started_at = 2000.0
    state.queue = ["x"]  # queue is preserved; only the flag is cleared
    await bridge._release_processing(state)
    assert state.processing is False
    assert state.started_at is None
    assert state.queue == ["x"]
