"""ClaudeSdkMopHarness — cc-sdk with MOP output filtering.

Wraps ClaudeSdkHarness. Buffers the full turn, runs the active MOP rules
against the assembled text just before yielding TurnFinal.

Filtering behaviour (current MVP — audit mode):
  - Accept: replay all buffered events + TurnFinal unchanged.
  - Reject/Edit: log the violation, replay all events + TurnFinal unchanged.
    The logged violation is the first-pass signal for rule calibration.

Promotion to enforcement (next step, requires bridge changes):
  - Reject → suppress TurnFinal, inject feedback to agent, expect retry turn.
  - Edit → rewrite TurnFinal.raw_text via Haiku, deliver rewritten version.

Configuration:
  MOP_RULES_DIR   — path to rules/active/ dir (defaults to sibling repo)
  MOP_LLM_BACKEND — llm backend: stub (default), haiku, gemma4
  MOP_LOG_PATH    — optional JSONL file for violation log
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from ..config import CLAUDE_PATH, MAX_TIMEOUT, MAX_TURNS, logger as bridge_logger
from .base import (
    CompactResult,
    ContextUsage,
    HarnessCapabilities,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)
from .claude_sdk import ClaudeSdkHarness

_MOP_REPO_DEFAULT = Path(__file__).resolve().parents[4] / "model-output-protocol"

_CAPABILITIES = HarnessCapabilities(
    supports_resume=True,
    supports_tool_streaming=True,
    supports_interrupt=True,
    supports_effort=True,
    supports_mcp=True,
    supports_inflight_push=False,   # buffered — can't push mid-buffer
    supports_context_query=True,
    supports_compact=True,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal inline rule loader + evaluator — no import from mop package yet.
# ---------------------------------------------------------------------------

def _rules_dir() -> Path:
    env = os.environ.get("MOP_RULES_DIR")
    if env:
        return Path(env)
    return _MOP_REPO_DEFAULT / "rules" / "active"


def _load_active_rules() -> list[dict]:
    rules_dir = _rules_dir()
    if not rules_dir.is_dir():
        logger.warning("MOP rules dir not found at %s — all turns pass", rules_dir)
        return []
    try:
        import yaml
    except ImportError:
        logger.warning("pyyaml not installed — MOP filter disabled")
        return []
    rules: list[dict] = []
    for path in sorted(rules_dir.rglob("*.yml")):
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        for entry in data.get("rules", []):
            rules.append(entry)
    return rules


def _eval_deterministic(rule: dict, text: str) -> bool:
    params = rule.get("parameters", {})
    dtype = params.get("type")
    if dtype == "regex":
        return any(re.search(pat, text) for pat in params.get("patterns", []))
    if dtype == "word_count":
        return len(text.split()) > params.get("max", 0)
    return False


async def _eval_llm(rule: dict, text: str, backend: str) -> bool:
    if backend == "stub":
        logger.debug("MOP LLM stub — %s always Accept", rule["name"])
        return False
    prompt = rule.get("parameters", {}).get("prompt", "")
    query = (
        f"{prompt.strip()}\n\nMessage:\n<message>\n{text}\n</message>\n\n"
        "Reply with JSON only: {\"violation\": true} or {\"violation\": false}."
    )
    if backend == "haiku":
        return await _haiku_eval(rule["name"], query)
    if backend == "gemma4":
        return await _gemma4_eval(rule["name"], query)
    logger.warning("Unknown MOP LLM backend %r — defaulting to Accept", backend)
    return False


async def _haiku_eval(rule_name: str, query: str) -> bool:
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic not installed — MOP LLM disabled for %s", rule_name)
        return False
    loop = asyncio.get_event_loop()
    client = anthropic.Anthropic()

    def _call() -> bool:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": query}],
        )
        raw = resp.content[0].text.strip()
        try:
            return bool(json.loads(raw).get("violation"))
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Haiku non-JSON for rule %s: %r", rule_name, raw[:60])
            return False

    return await loop.run_in_executor(None, _call)


async def _gemma4_eval(rule_name: str, query: str) -> bool:
    import urllib.request
    body = json.dumps({"model": "gemma4:e4b", "prompt": query, "stream": False}).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    loop = asyncio.get_event_loop()

    def _call() -> bool:
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            raw = data.get("response", "").strip()
            return bool(json.loads(raw).get("violation"))
        except Exception as exc:
            logger.warning("Gemma4 eval failed for %s: %s", rule_name, exc)
            return False

    return await loop.run_in_executor(None, _call)


# ---------------------------------------------------------------------------
# Violation log
# ---------------------------------------------------------------------------

def _log_violation(session_key: str, rule: dict, text: str) -> None:
    log_path = os.environ.get("MOP_LOG_PATH")
    entry = {
        "session_key": session_key,
        "rule": rule["name"],
        "on_violation": rule.get("on_violation", "warn"),
        "severity": rule.get("severity", "warn"),
        "text_preview": text[:200],
    }
    logger.info("MOP violation: %s", json.dumps(entry))
    if log_path:
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class ClaudeSdkMopHarness:
    """cc-sdk with MOP output filtering (audit mode for MVP)."""

    name = "cc-sdk-mop"
    capabilities = _CAPABILITIES

    def __init__(
        self,
        *,
        cli_path: str = CLAUDE_PATH,
        max_timeout_seconds: float = MAX_TIMEOUT,
        max_turns_default: int = MAX_TURNS,
        on_progress=None,
        llm_backend: Literal["stub", "haiku", "gemma4"] = "stub",
    ) -> None:
        self._inner = ClaudeSdkHarness(
            cli_path=cli_path,
            max_timeout_seconds=max_timeout_seconds,
            max_turns_default=max_turns_default,
            on_progress=on_progress,
        )
        env_backend = os.environ.get("MOP_LLM_BACKEND", llm_backend)
        self._llm_backend: str = env_backend
        self._rules: list[dict] | None = None

    def _get_rules(self) -> list[dict]:
        if self._rules is None:
            self._rules = _load_active_rules()
        return self._rules

    async def _evaluate(self, text: str) -> tuple[dict | None, str]:
        """Return (first_violating_rule, action) or (None, 'accept')."""
        for rule in self._get_rules():
            detector = rule.get("detector", "")
            fired = False
            if detector == "deterministic":
                fired = _eval_deterministic(rule, text)
            elif detector == "llm":
                fired = await _eval_llm(rule, text, self._llm_backend)
            if fired:
                action = rule.get("on_violation", "warn")
                return rule, action
        return None, "accept"

    async def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Buffer full turn, run MOP at TurnFinal."""
        buffered: list[TurnEvent] = []
        final: TurnFinal | TurnError | None = None

        async for event in self._inner.run_turn(req):
            if isinstance(event, (TurnFinal, TurnError)):
                final = event
            else:
                buffered.append(event)

        # Always replay non-terminal events first.
        for event in buffered:
            yield event

        if final is None:
            logger.warning("cc-sdk-mop: inner harness yielded no terminal event for %s", req.session_key)
            yield TurnError(
                kind="unknown",
                message="MOP wrapper: inner harness produced no terminal event",
                retryable=True,
                metadata={},
            )
            return

        if isinstance(final, TurnFinal):
            violated_rule, action = await self._evaluate(final.raw_text)
            if violated_rule is not None:
                _log_violation(req.session_key, violated_rule, final.raw_text)
                # MVP: audit only — log the violation but deliver unchanged.
                # TODO: when bridge supports it, suppress + inject feedback for reject.

        yield final

    async def cancel(self) -> None:
        await self._inner.cancel()

    async def get_context(self, req: TurnRequest) -> ContextUsage:
        return await self._inner.get_context(req)

    async def compact(self, req: TurnRequest, instructions: str | None = None) -> CompactResult:
        return await self._inner.compact(req, instructions=instructions)
