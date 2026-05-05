"""Tests for ClaudeSdkMopHarness — MOP output filter wrapper.

Stubs out the inner ClaudeSdkHarness so tests don't touch the claude CLI.
Verifies: buffering, pass-through on Accept, violation logging on Reject,
configurable LLM backend stub, enforce mode retry loop, edit/rewrite,
empty-message structural check, and that TurnError from inner harness
passes through without MOP evaluation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

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


def _make_harness(events_seq, *, llm_backend: str = "stub") -> ClaudeSdkMopHarness:
    harness = ClaudeSdkMopHarness(llm_backend=llm_backend)
    inner = MagicMock()
    inner.run_turn = MagicMock(return_value=_events(*events_seq))
    harness._inner = inner
    return harness


def _make_harness_multi(calls: list[list], *, llm_backend: str = "stub") -> ClaudeSdkMopHarness:
    """Harness whose inner.run_turn returns a different event sequence each call."""
    harness = ClaudeSdkMopHarness(llm_backend=llm_backend)
    iterators = [_events(*seq) for seq in calls]
    side_effect = iter(iterators)
    inner = MagicMock()
    inner.run_turn = MagicMock(side_effect=lambda _req: next(side_effect))
    harness._inner = inner
    return harness


async def _collect(harness: ClaudeSdkMopHarness, req: TurnRequest) -> list:
    results = []
    async for event in harness.run_turn(req):
        results.append(event)
    return results


# ---------------------------------------------------------------------------
# Passthrough mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_passthrough_mode_bypasses_all_eval(monkeypatch):
    """passthrough mode delivers all events without evaluation."""
    monkeypatch.setenv("MOP_MODE", "passthrough")
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="")
    harness = _make_harness([TextDelta(text="x"), final])
    events = await _collect(harness, _req())
    assert len(events) == 2
    assert isinstance(events[-1], TurnFinal)


# ---------------------------------------------------------------------------
# Audit mode (default)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accept_passes_all_events_through(monkeypatch):
    """Non-violating turn: all events including TurnFinal reach the caller."""
    monkeypatch.setenv("MOP_MODE", "audit")
    final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=0.001, raw_text="Done.")
    harness = _make_harness([TextDelta(text="Do"), TextDelta(text="ne."), final])
    events = await _collect(harness, _req())
    assert len(events) == 3
    assert events[-1] is final


@pytest.mark.asyncio
async def test_tool_use_events_pass_through(monkeypatch):
    """ToolUse events buffer and replay before TurnFinal."""
    monkeypatch.setenv("MOP_MODE", "audit")
    tool = ToolUse(name="Read", input={"file_path": "/foo"}, id="t1")
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="read it")
    harness = _make_harness([tool, final])
    events = await _collect(harness, _req())
    assert events[0] is tool
    assert events[1] is final


@pytest.mark.asyncio
async def test_turn_error_passes_through_without_mop(monkeypatch):
    """If inner harness errors, MOP does not evaluate — error passes through."""
    monkeypatch.setenv("MOP_MODE", "audit")
    err = TurnError(kind="timeout", message="timed out", retryable=True, metadata={})
    harness = _make_harness([err])
    events = await _collect(harness, _req())
    assert len(events) == 1
    assert isinstance(events[0], TurnError)
    assert events[0].kind == "timeout"


@pytest.mark.asyncio
async def test_violation_logged_to_jsonl(tmp_path, monkeypatch):
    """Audit mode: on a violation, entry is appended to MOP_LOG_PATH."""
    import threading as _threading
    import patchbay.harness.claude_sdk_mop as _mop_mod

    monkeypatch.setenv("MOP_MODE", "audit")
    log_file = str(tmp_path / "violations.jsonl")
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="Want me to fix it?")
    harness = _make_harness([final], llm_backend="haiku")
    monkeypatch.setenv("MOP_LOG_PATH", log_file)
    monkeypatch.setenv("MOP_LLM_BACKEND", "haiku")
    harness._llm_backend = "haiku"
    harness._rules = [{
        "name": "no-permission-asking-for-doable-work",
        "detector": "llm",
        "on_violation": "warn",
        "severity": "warn",
        "parameters": {"prompt": "Does this message ask permission?"},
    }]

    monkeypatch.setattr(_mop_mod, "_claude_p_eval", lambda rule_name, query: True)

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
async def test_no_violation_no_log(tmp_path, monkeypatch):
    """Audit mode: clean turn does not create the violation log file."""
    monkeypatch.setenv("MOP_MODE", "audit")
    log_file = str(tmp_path / "violations.jsonl")
    monkeypatch.setenv("MOP_LOG_PATH", log_file)
    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="Here is the result.")
    harness = _make_harness([final])
    await _collect(harness, _req())
    assert not Path(log_file).exists()


# ---------------------------------------------------------------------------
# Enforce mode — structural empty-message check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enforce_empty_message_retries(monkeypatch):
    """Enforce mode: empty raw_text triggers reject + retry."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    monkeypatch.setenv("MOP_MAX_RETRIES", "2")

    empty_final = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="")
    good_final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="Here you go.")

    harness = _make_harness_multi([[empty_final], [good_final]])
    harness._rules = []  # no rules, only structural check

    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    assert events[0].raw_text == "Here you go."


@pytest.mark.asyncio
async def test_enforce_empty_message_max_retries_delivers_last(monkeypatch):
    """Enforce mode: max retries exhausted on empty → deliver last turn anyway."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    monkeypatch.setenv("MOP_MAX_RETRIES", "2")

    empty1 = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="")
    empty2 = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="   ")

    harness = _make_harness_multi([[empty1], [empty2]])
    harness._rules = []

    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    # Delivered the last attempt's TurnFinal despite violation
    assert events[0].raw_text == "   "


# ---------------------------------------------------------------------------
# Enforce mode — Reject verdict
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enforce_reject_retries_and_delivers_clean(monkeypatch):
    """Enforce mode: Reject verdict on first attempt, clean second attempt delivered."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    monkeypatch.setenv("MOP_MAX_RETRIES", "3")
    import patchbay.harness.claude_sdk_mop as _mop_mod

    bad_final = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="Want me to fix that?")
    good_final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="Fixed it.")

    harness = _make_harness_multi([[bad_final], [good_final]])
    harness._rules = [{
        "name": "no-permission-asking-for-doable-work",
        "detector": "llm",
        "on_violation": "reject",
        "severity": "violation",
        "guidance": "Don't ask permission — just do the work.",
        "parameters": {"prompt": "Does this ask permission?"},
    }]

    call_count = {"n": 0}
    def _eval_stub(rule_name, query):
        call_count["n"] += 1
        return call_count["n"] == 1  # first call fires, second doesn't

    monkeypatch.setattr(_mop_mod, "_claude_p_eval", _eval_stub)
    harness._llm_backend = "haiku"

    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    assert events[0].raw_text == "Fixed it."


@pytest.mark.asyncio
async def test_enforce_reject_logs_violation(tmp_path, monkeypatch):
    """Enforce mode: Reject violation is written to log before retry."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    monkeypatch.setenv("MOP_MAX_RETRIES", "2")
    log_file = str(tmp_path / "v.jsonl")
    monkeypatch.setenv("MOP_LOG_PATH", log_file)

    import patchbay.harness.claude_sdk_mop as _mop_mod

    bad_final = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="Want me to help?")
    good_final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="Done.")

    harness = _make_harness_multi([[bad_final], [good_final]])
    harness._rules = [{
        "name": "no-permission-asking",
        "detector": "llm",
        "on_violation": "reject",
        "severity": "violation",
        "guidance": "Just do it.",
        "parameters": {"prompt": "Does it ask permission?"},
    }]
    monkeypatch.setattr(_mop_mod, "_claude_p_eval", lambda *_: True)
    harness._llm_backend = "haiku"

    # Patch second eval to return False so it cleans up
    call_count = {"n": 0}
    def _staged_eval(*_):
        call_count["n"] += 1
        return call_count["n"] == 1
    monkeypatch.setattr(_mop_mod, "_claude_p_eval", _staged_eval)

    await _collect(harness, _req())

    assert Path(log_file).exists()
    line = json.loads(Path(log_file).read_text().strip())
    assert line["rule"] == "no-permission-asking"


# ---------------------------------------------------------------------------
# Enforce mode — Edit verdict (rewrite)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enforce_edit_rewrites_output(monkeypatch):
    """Enforce mode: Edit verdict rewrites raw_text before delivery."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    import patchbay.harness.claude_sdk_mop as _mop_mod

    long_final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="A " * 300)

    harness = _make_harness([long_final])
    harness._rules = [{
        "name": "length-cap-chat",
        "detector": "deterministic",
        "on_violation": "edit",
        "severity": "warn",
        "guidance": "Shorten to under 200 words.",
        "parameters": {"type": "word_count", "max": 200},
    }]

    monkeypatch.setattr(_mop_mod, "_claude_p_rewrite", lambda _prompt: "Short reply.")

    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    assert events[0].raw_text == "Short reply."


@pytest.mark.asyncio
async def test_enforce_edit_fallback_on_rewrite_failure(monkeypatch):
    """Enforce mode: Edit verdict falls back to original if rewrite fails."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    import patchbay.harness.claude_sdk_mop as _mop_mod

    long_final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="word " * 250)

    harness = _make_harness([long_final])
    harness._rules = [{
        "name": "length-cap-chat",
        "detector": "deterministic",
        "on_violation": "edit",
        "severity": "warn",
        "parameters": {"type": "word_count", "max": 200},
    }]
    monkeypatch.setattr(_mop_mod, "_claude_p_rewrite", lambda _prompt: "")  # rewrite fails

    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    assert events[0].raw_text == "word " * 250  # original


# ---------------------------------------------------------------------------
# Registration
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
    assert caps.supports_inflight_push is False
