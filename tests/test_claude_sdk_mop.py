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
async def test_violation_logged_to_jsonl(tmp_path, monkeypatch):
    """On a violation, an entry is appended to MOP_LOG_PATH."""
    import threading as _threading
    import patchbay.harness.claude_sdk_mop as _mop_mod

    log_file = str(tmp_path / "violations.jsonl")
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="Want me to fix it?")
    harness = _make_harness([final])
    monkeypatch.setenv("MOP_LOG_PATH", log_file)
    monkeypatch.setenv("MOP_LLM_BACKEND", "haiku")
    harness._llm_backend = "haiku"
    # Inject a minimal rule so the test doesn't depend on the rules dir path.
    harness._rules = [{
        "name": "no-permission-asking-for-doable-work",
        "detector": "llm",
        "on_violation": "warn",
        "severity": "warn",
        "parameters": {"prompt": "Does this message ask permission?"},
    }]

    # Patch _claude_p_eval to return True so the violation fires without CLI.
    monkeypatch.setattr(_mop_mod, "_claude_p_eval", lambda rule_name, query: True)

    # Make threading.Thread run synchronously so the test doesn't race.
    class _SyncThread:
        def __init__(self, target, args=(), daemon=False, **_):
            self._target, self._args = target, args
        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(_threading, "Thread", _SyncThread)

    events = await _collect(harness, _req())

    assert Path(log_file).exists()
    line = json.loads(Path(log_file).read_text().strip())
    assert line["rule"] == "no-permission-asking-for-doable-work"
    assert "Want me to fix it?" in line["text_preview"]
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
