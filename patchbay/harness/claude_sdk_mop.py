"""ClaudeSdkMopHarness — cc-sdk with MOP output filtering.

Wraps ClaudeSdkHarness. Buffers the full turn, runs the active MOP rules
against the assembled text just before yielding TurnFinal.

Modes (set via MOP_MODE env var):
  passthrough  — never evaluate, always deliver unchanged
  audit        — evaluate async after delivery, log violations, never block
  enforce      — evaluate synchronously before delivery; Reject suppresses
                 TurnFinal and injects guidance for a retry turn; Edit
                 rewrites via Haiku before delivery (default: audit)

Reject retry loop:
  - On Reject verdict: discard buffered events, inject MOP guidance as new
    prompt (resuming same session), retry up to MOP_MAX_RETRIES times.
  - On max-retry exhaustion: deliver the last turn unchanged (with log).

Edit rewrite:
  - Haiku rewrites raw_text in-place. Original text is logged. Delivery
    uses the rewritten TurnFinal.

Structural check:
  - Empty raw_text is always treated as Reject regardless of mode.

Configuration:
  MOP_MODE        — passthrough | audit | enforce (default: audit)
  MOP_RULES_DIR   — path to rules/active/ dir (defaults to sibling repo)
  MOP_LLM_BACKEND — llm backend: stub (default), haiku, gemma4
  MOP_LOG_PATH    — optional JSONL file for violation log
  MOP_MAX_RETRIES — max retry attempts in enforce mode (default: 3)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
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

_MOP_REPO_DEFAULT = Path(__file__).resolve().parents[4] / "Developer" / "model-output-protocol"

_CAPABILITIES = HarnessCapabilities(
    supports_resume=True,
    supports_tool_streaming=True,
    supports_interrupt=True,
    supports_effort=True,
    supports_mcp=True,
    # False: MOP buffers all TextDelta until TurnFinal before evaluating.
    # Channels stream partial output — incompatible. Intended fix: audit-only
    # in channel mode (no blocking), then streaming deterministic eval.
    # See model-output-protocol/docs/architecture.md §Channels compatibility.
    supports_inflight_push=False,
    supports_context_query=True,
    supports_compact=True,
)

logger = logging.getLogger(__name__)

_EMPTY_MESSAGE_RULE: dict = {
    "name": "empty-message",
    "on_violation": "reject",
    "severity": "violation",
    "guidance": (
        "Your response was empty. You must send at least one text message per turn. "
        "Write a response and try again."
    ),
}


# ---------------------------------------------------------------------------
# Rule loader
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


# ---------------------------------------------------------------------------
# Deterministic evaluators
# ---------------------------------------------------------------------------

def _eval_deterministic(rule: dict, text: str) -> bool:
    params = rule.get("parameters", {})
    dtype = params.get("type")
    if dtype == "regex":
        return any(re.search(pat, text) for pat in params.get("patterns", []))
    if dtype == "word_count":
        return len(text.split()) > params.get("max", 0)
    return False


# ---------------------------------------------------------------------------
# LLM evaluator — claude -p subprocess
# ---------------------------------------------------------------------------

def _claude_p_eval(rule_name: str, query: str) -> bool:
    """One-shot eval via `claude -p`. Runs under Max plan — no API billing.
    Runs in /tmp with no cwd context so project CLAUDE.md/hooks don't fire."""
    cli = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    try:
        result = subprocess.run(
            [cli, "-p", query, "--max-turns", "1"],
            capture_output=True, text=True, timeout=30,
            cwd="/tmp",
        )
        raw = result.stdout.strip()
        return bool(json.loads(raw).get("violation"))
    except Exception as exc:
        logger.warning("claude -p eval failed for rule %s: %s — Accept", rule_name, exc)
        return False


def _claude_p_rewrite(prompt: str) -> str:
    """One-shot rewrite via `claude -p`. Returns rewritten text, or empty on failure."""
    cli = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    try:
        result = subprocess.run(
            [cli, "-p", prompt, "--max-turns", "1"],
            capture_output=True, text=True, timeout=30,
            cwd="/tmp",
        )
        return result.stdout.strip()
    except Exception as exc:
        logger.warning("claude -p rewrite failed: %s", exc)
        return ""


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
# Synchronous evaluation — returns (verdict, fired_rule | None)
# ---------------------------------------------------------------------------

def _evaluate_turn(
    session_key: str,
    text: str,
    rules: list[dict],
    backend: str,
) -> tuple[str, dict | None]:
    """Evaluate text against all active rules synchronously.

    Returns:
        ("accept", None)            — no violation
        ("reject", rule_dict)       — rule with on_violation=reject fired
        ("edit", rule_dict)         — rule with on_violation=edit fired
    """
    if not text.strip():
        return "reject", _EMPTY_MESSAGE_RULE

    for rule in rules:
        detector = rule.get("detector", "")
        fired = False

        if detector == "deterministic":
            fired = _eval_deterministic(rule, text)
        elif detector == "llm" and backend in ("haiku", "gemma4"):
            prompt = rule.get("parameters", {}).get("prompt", "")
            query = (
                f"{prompt.strip()}\n\nMessage to evaluate:\n<message>\n{text}\n</message>\n\n"
                "Reply with JSON only: {\"violation\": true} or {\"violation\": false}."
            )
            if backend == "haiku":
                fired = _claude_p_eval(rule["name"], query)
            # gemma4 skipped in sync path — needs async, falls back to skip

        if fired:
            on_violation = rule.get("on_violation", "warn")
            if on_violation == "reject":
                return "reject", rule
            if on_violation == "edit":
                return "edit", rule
            # warn: log but continue checking remaining rules

    return "accept", None


# ---------------------------------------------------------------------------
# Audit-mode async eval (fire-and-forget from daemon thread)
# ---------------------------------------------------------------------------

def _eval_and_log_sync(session_key: str, text: str, rules: list[dict], backend: str) -> None:
    """Evaluate and log any violations. Designed for daemon threads — never blocks response."""
    for rule in rules:
        detector = rule.get("detector", "")
        fired = False
        if detector == "deterministic":
            fired = _eval_deterministic(rule, text)
        elif detector == "llm":
            if backend in ("haiku", "gemma4"):
                prompt = rule.get("parameters", {}).get("prompt", "")
                query = (
                    f"{prompt.strip()}\n\nMessage to evaluate:\n<message>\n{text}\n</message>\n\n"
                    "Reply with JSON only: {\"violation\": true} or {\"violation\": false}."
                )
                fired = _claude_p_eval(rule["name"], query)
        if fired:
            _log_violation(session_key, rule, text)
            return


# ---------------------------------------------------------------------------
# Edit/Rewrite
# ---------------------------------------------------------------------------

def _rewrite_sync(text: str, rule: dict, backend: str) -> str:
    """Rewrite text to fix a style violation. Returns original on failure."""
    rule_name = rule.get("name", "unknown")
    guidance = rule.get("guidance") or rule.get("parameters", {}).get("prompt", "")

    rewrite_prompt = (
        f"You are a text editor. Rewrite the following message to fix this style violation.\n\n"
        f"Rule: {rule_name}\n"
        f"Guidance: {guidance}\n\n"
        f"Rewrite the message to fix the violation while preserving ALL substantive content.\n"
        f"Output ONLY the rewritten message, nothing else.\n\n"
        f"Original message:\n<message>\n{text}\n</message>"
    )

    if backend in ("haiku", "gemma4"):
        rewritten = _claude_p_rewrite(rewrite_prompt)
        if rewritten:
            logger.info("MOP rewrite: rule=%s original_len=%d rewritten_len=%d", rule_name, len(text), len(rewritten))
            return rewritten

    logger.warning("MOP rewrite failed for rule %s backend=%s — delivering original", rule_name, backend)
    return text


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class ClaudeSdkMopHarness:
    """cc-sdk with MOP output filtering (audit + enforce modes)."""

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
        self._llm_backend: str = os.environ.get("MOP_LLM_BACKEND", llm_backend)
        self._rules: list[dict] | None = None

    def _get_rules(self) -> list[dict]:
        if self._rules is None:
            self._rules = _load_active_rules()
        return self._rules

    @staticmethod
    def _mode() -> str:
        return os.environ.get("MOP_MODE", "audit")

    async def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]:
        mode = self._mode()

        if mode == "passthrough":
            async for event in self._inner.run_turn(req):
                yield event
            return

        rules = list(self._get_rules())
        backend = self._llm_backend
        max_retries = int(os.environ.get("MOP_MAX_RETRIES", "3"))
        current_req = req

        for attempt in range(max_retries if mode == "enforce" else 1):
            buffered: list[TurnEvent] = []
            final: TurnFinal | TurnError | None = None

            async for event in self._inner.run_turn(current_req):
                if isinstance(event, (TurnFinal, TurnError)):
                    final = event
                else:
                    buffered.append(event)

            if final is None:
                logger.warning("cc-sdk-mop: inner harness yielded no terminal event for %s", req.session_key)
                yield TurnError(
                    kind="unknown",
                    message="MOP wrapper: inner harness produced no terminal event",
                    retryable=True,
                    metadata={},
                )
                return

            if isinstance(final, TurnError):
                yield final
                return

            # --- Audit mode: fire-and-forget, always deliver ---
            if mode == "audit":
                for event in buffered:
                    yield event
                yield final
                threading.Thread(
                    target=_eval_and_log_sync,
                    args=(req.session_key, final.raw_text, rules, backend),
                    daemon=True,
                ).start()
                return

            # --- Enforce mode: synchronous eval before delivery ---
            verdict, fired_rule = _evaluate_turn(req.session_key, final.raw_text, rules, backend)

            if verdict == "accept":
                for event in buffered:
                    yield event
                yield final
                return

            # Violation in enforce mode
            _log_violation(req.session_key, fired_rule, final.raw_text)
            rule_name = fired_rule.get("name", "unknown")

            if verdict == "edit":
                rewritten_text = _rewrite_sync(final.raw_text, fired_rule, backend)
                rewritten_final = TurnFinal(
                    session_id=final.session_id,
                    num_turns=final.num_turns,
                    total_cost_usd=final.total_cost_usd,
                    raw_text=rewritten_text,
                )
                for event in buffered:
                    yield event
                yield rewritten_final
                return

            # Reject: discard buffered events, inject guidance, retry
            if attempt < max_retries - 1:
                guidance = fired_rule.get("guidance") or fired_rule.get("parameters", {}).get("prompt", "")
                inject_prompt = (
                    f"[MOP feedback — attempt {attempt + 1}/{max_retries}] "
                    f"Your previous response violated rule '{rule_name}'. "
                    f"{guidance} "
                    f"Revise and try again."
                )
                logger.info(
                    "MOP reject: session=%s rule=%s attempt=%d/%d",
                    req.session_key, rule_name, attempt + 1, max_retries,
                )
                current_req = TurnRequest(
                    prompt=inject_prompt,
                    session_key=req.session_key,
                    project_dir=req.project_dir,
                    system_prompt=req.system_prompt,
                    resume_session_id=final.session_id,
                    model=req.model,
                    effort=req.effort,
                    allowed_tools=req.allowed_tools,
                    disallowed_tools=req.disallowed_tools,
                    max_turns=req.max_turns,
                    plugin_dir=req.plugin_dir,
                    extra=req.extra,
                )
                # buffered events from rejected turn are discarded (not shown to user)
                continue

            # Max retries exhausted — deliver last turn with warning
            logger.warning(
                "MOP max retries exhausted: session=%s rule=%s — delivering despite violation",
                req.session_key, rule_name,
            )
            for event in buffered:
                yield event
            yield final
            return

        # Should not reach here
        yield TurnError(
            kind="unknown",
            message="MOP: retry loop exhausted without delivering",
            retryable=False,
            metadata={},
        )

    async def cancel(self) -> None:
        await self._inner.cancel()

    async def get_context(self, req: TurnRequest) -> ContextUsage:
        return await self._inner.get_context(req)

    async def compact(self, req: TurnRequest, instructions: str | None = None) -> CompactResult:
        return await self._inner.compact(req, instructions=instructions)
