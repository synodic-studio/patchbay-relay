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
    import patchbay.harness.claude_sdk_mop as _mop_mod
    from mop import Action, Verdict

    monkeypatch.setenv("MOP_MODE", "audit")
    log_file = str(tmp_path / "violations.jsonl")
    monkeypatch.setenv("MOP_LOG_PATH", log_file)

    final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="Want me to fix it?")
    harness = _make_harness([final])

    async def _stub_evaluate(text, config=None):
        return Verdict(action=Action.REJECT, rule="no-permission-asking-for-doable-work")

    monkeypatch.setattr(_mop_mod, "evaluate",_stub_evaluate)

    events = await _collect(harness, _req())
    assert isinstance(events[-1], TurnFinal)

    # Daemon thread runs evaluate in its own event loop — wait for it.
    import time
    deadline = time.time() + 2.0
    while not Path(log_file).exists() and time.time() < deadline:
        await asyncio.sleep(0.05)

    assert Path(log_file).exists()
    line = json.loads(Path(log_file).read_text().strip())
    assert line["rule"] == "no-permission-asking-for-doable-work"
    assert "Want me to fix it?" in line["text_preview"]


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
async def test_enforce_empty_message_retries(monkeypatch, tmp_path):
    """Enforce mode: empty raw_text triggers reject + retry (built-in empty check in mop.filter)."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    monkeypatch.setenv("MOP_MAX_RETRIES", "2")
    monkeypatch.setenv("MOP_RULES_DIR", str(tmp_path))  # empty dir — no yaml rules

    empty_final = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="")
    good_final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="Here you go.")

    harness = _make_harness_multi([[empty_final], [good_final]])
    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    assert events[0].raw_text == "Here you go."


@pytest.mark.asyncio
async def test_enforce_empty_message_max_retries_delivers_last(monkeypatch, tmp_path):
    """Enforce mode: max retries exhausted on empty → deliver last turn anyway."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    monkeypatch.setenv("MOP_MAX_RETRIES", "2")
    monkeypatch.setenv("MOP_RULES_DIR", str(tmp_path))

    empty1 = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="")
    empty2 = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="   ")

    harness = _make_harness_multi([[empty1], [empty2]])
    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
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
    from mop import Action, Verdict

    bad_final = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="Want me to fix that?")
    good_final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="Fixed it.")
    harness = _make_harness_multi([[bad_final], [good_final]])

    call_count = {"n": 0}

    async def _stub_evaluate(text, config=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return Verdict(action=Action.REJECT, rule="no-permission-asking-for-doable-work",
                           guidance="Don't ask permission.")
        return Verdict(action=Action.ACCEPT)

    monkeypatch.setattr(_mop_mod, "evaluate",_stub_evaluate)
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
    from mop import Action, Verdict

    bad_final = TurnFinal(session_id="s1", num_turns=1, total_cost_usd=None, raw_text="Want me to help?")
    good_final = TurnFinal(session_id="s1", num_turns=2, total_cost_usd=None, raw_text="Done.")
    harness = _make_harness_multi([[bad_final], [good_final]])

    call_count = {"n": 0}

    async def _staged_evaluate(text, config=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return Verdict(action=Action.REJECT, rule="no-permission-asking")
        return Verdict(action=Action.ACCEPT)

    monkeypatch.setattr(_mop_mod, "evaluate",_staged_evaluate)
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
    import patchbay.harness.claude_sdk_mop as _mop_mod
    from mop import Action, Verdict

    long_final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text="A " * 300)
    harness = _make_harness([long_final])

    async def _stub_evaluate(text, config=None):
        return Verdict(action=Action.EDIT, rule="length-cap-chat", guidance="Shorten to under 200 words.")

    async def _stub_rewrite(text, rule_name, guidance):
        return "Short reply."

    monkeypatch.setattr(_mop_mod, "evaluate",_stub_evaluate)
    monkeypatch.setattr(_mop_mod, "mop_rewrite",_stub_rewrite)

    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    assert events[0].raw_text == "Short reply."


@pytest.mark.asyncio
async def test_enforce_edit_fallback_on_rewrite_failure(monkeypatch):
    """Enforce mode: Edit verdict falls back to original if rewrite returns original."""
    monkeypatch.setenv("MOP_MODE", "enforce")
    import patchbay.harness.claude_sdk_mop as _mop_mod
    import patchbay.harness.claude_sdk_mop as _mop_mod
    from mop import Action, Verdict

    original_text = "word " * 250
    long_final = TurnFinal(session_id=None, num_turns=1, total_cost_usd=None, raw_text=original_text)
    harness = _make_harness([long_final])

    async def _stub_evaluate(text, config=None):
        return Verdict(action=Action.EDIT, rule="length-cap-chat", guidance="Shorten.")

    async def _stub_rewrite(text, rule_name, guidance):
        return text  # rewrite returns original (failure fallback)

    monkeypatch.setattr(_mop_mod, "evaluate",_stub_evaluate)
    monkeypatch.setattr(_mop_mod, "mop_rewrite",_stub_rewrite)

    events = await _collect(harness, _req())

    assert len(events) == 1
    assert isinstance(events[0], TurnFinal)
    assert events[0].raw_text == original_text


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
