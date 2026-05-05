"""Tests for ClaudeSdkMopHarness — MOP output filter wrapper.

Stubs out the inner ClaudeSdkHarness so tests don't touch the claude CLI.
Verifies: buffering, pass-through on Accept, violation logging on Reject,
configurable LLM backend stub, and that TurnError from inner harness
passes through without MOP evaluation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patchbay.harness.base import (
    TextDelta,
    ToolUse,
    TurnError,
    TurnFinal,
    TurnRequest,
)
from patchbay.harness.claude_sdk_mop import ClaudeSdkMopHarness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(session_key: str = "test_session") -> TurnRequest:
    return TurnRequest(
        prompt="test",
        session_key=session_key,
        project_dir=Path("/tmp"),
        system_prompt=None,
        resume_session_id=None,
        model=None,
        effort=None,
        allowed_tools=None,
        disallowed_tools=None,
        max_turns=10,
        plugin_dir=None,
    )


async def _events(*items) -> AsyncIterator:
    for item in items:
        yield item


def _make_harness(events, *, llm_backend: str = "stub", log_path: str | None = None, monkeypatch=None) -> ClaudeSdkMopHarness:
    harness = ClaudeSdkMopHarness(llm_backend=llm_backend)
    if log_path and monkeypatch:
        monkeypatch.setenv("MOP_LOG_PATH", log_path)
    inner = MagicMock()
    inner.run_turn = MagicMock(return_value=_events(*events))
    harness._inner = inner
    return harness


async def _collect(harness: ClaudeSdkMopHarness, req: TurnRequest) -> list:
    results = []
    async for event in harness.run_turn(req):
        results.append(event)
    return results


# ---------------------------------------------------------------------------
# Pass-through: no violations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accept_passes_all_events_through():
    """Non-violating turn: all events including TurnFinal reach the caller."""
    final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=0.001, raw_text="Done.")
    harness = _make_harness([
        TextDelta(text="Do"),
        TextDelta(text="ne."),
        final,
    ])
    events = await _collect(harness, _req())
    assert len(events) == 3
    assert events[-1] is final


@pytest.mark.asyncio
async def test_tool_use_events_pass_through():
    """ToolUse events buffer and replay before TurnFinal."""
    tool = ToolUse(name="Read", input={"file_path": "/foo"}, id="t1")
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="read it")
    harness = _make_harness([tool, final])
    events = await _collect(harness, _req())
    assert events[0] is tool
    assert events[1] is final


# ---------------------------------------------------------------------------
# TurnError from inner harness passes through without MOP evaluation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn_error_passes_through_without_mop():
    """If inner harness errors, MOP does not try to evaluate — error passes through."""
    err = TurnError(kind="timeout", message="timed out", retryable=True, metadata={})
    harness = _make_harness([err])
    events = await _collect(harness, _req())
    assert len(events) == 1
    assert isinstance(events[0], TurnError)
    assert events[0].kind == "timeout"


# ---------------------------------------------------------------------------
# Violation logging (stub backend always Accept; patch _evaluate for Reject)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_violation_logged_to_jsonl(tmp_path):
    """On a violation, an entry is appended to MOP_LOG_PATH."""
    log_file = str(tmp_path / "violations.jsonl")
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="Want me to fix it?")
    harness = _make_harness([final])

    import os
    os.environ["MOP_LOG_PATH"] = log_file

    # Force a violation by patching _evaluate
    async def _fake_evaluate(text):
        return ({"name": "no-permission-asking-for-doable-work", "on_violation": "reject", "severity": "violation", "guidance": ""}, "reject")

    harness._evaluate = _fake_evaluate

    try:
        events = await _collect(harness, _req())
    finally:
        os.environ.pop("MOP_LOG_PATH", None)

    assert Path(log_file).exists()
    line = json.loads(Path(log_file).read_text().strip())
    assert line["rule"] == "no-permission-asking-for-doable-work"
    assert "Want me to fix it?" in line["text_preview"]
    # MVP: audit mode — TurnFinal still delivered
    assert isinstance(events[-1], TurnFinal)


@pytest.mark.asyncio
async def test_no_violation_no_log(tmp_path):
    """Clean turn: violation log file is not created."""
    log_file = str(tmp_path / "violations.jsonl")
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="Here is the result.")
    harness = _make_harness([final])

    import os
    os.environ["MOP_LOG_PATH"] = log_file

    try:
        await _collect(harness, _req())
    finally:
        os.environ.pop("MOP_LOG_PATH", None)

    assert not Path(log_file).exists()


# ---------------------------------------------------------------------------
# cc-sdk-mop in VALID_HARNESSES and CAPABILITIES_BY_NAME
# ---------------------------------------------------------------------------

def test_cc_sdk_mop_registered():
    from patchbay.config import VALID_HARNESSES
    from patchbay.harness import CAPABILITIES_BY_NAME
    assert "cc-sdk-mop" in VALID_HARNESSES
    assert "cc-sdk-mop" in CAPABILITIES_BY_NAME


def test_cc_sdk_mop_capabilities():
    from patchbay.harness import CAPABILITIES_BY_NAME
    caps = CAPABILITIES_BY_NAME["cc-sdk-mop"]
    assert caps.supports_resume is True
    assert caps.supports_interrupt is True
    # Buffered mode — no inflight push support
    assert caps.supports_inflight_push is False
