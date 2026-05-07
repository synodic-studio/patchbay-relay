"""ClaudeSdkMopHarness — cc-sdk with MOP output filtering.

Wraps ClaudeSdkHarness. Buffers the full turn, runs MOP evaluation
against the assembled text just before yielding TurnFinal.

Modes (set via MOP_MODE env var):
  passthrough  — never evaluate, always deliver unchanged
  audit        — evaluate async after delivery, log violations, never block
  enforce      — evaluate synchronously before delivery; Reject suppresses
                 TurnFinal and injects guidance for a retry turn; Edit
                 rewrites via pydantic-ai before delivery (default: audit)

Reject retry loop:
  On Reject verdict: discard buffered events, inject MOP guidance as a new
  prompt (resuming the same session), retry up to MOP_MAX_RETRIES times.
  On max-retry exhaustion: deliver the last turn unchanged (with log).

Configuration:
  MOP_MODE        — passthrough | audit | enforce (default: audit)
  MOP_RULES_DIR   — path to rules/active/ dir (defaults to sibling repo)
  MOP_LLM_BACKEND — llm backend: stub (default), haiku, gemma4
  MOP_LOG_PATH    — optional JSONL file for violation log
  MOP_MAX_RETRIES — max retry attempts in enforce mode (default: 3)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Literal

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from mop import MOP, Action, MopConfig, build_mcp_server, evaluate, protocol_prompt
from mop import rewrite as mop_rewrite
from mop import stop as mop_stop
from mop.rules import load_rules
from mop.types import Block

from ..config import CLAUDE_PATH, MAX_TIMEOUT, MAX_TURNS
from ..mop_deliver import build_telegram_deliver
from ..mop_evaluator import build_haiku_evaluator
from .base import (
    CompactResult,
    ContextUsage,
    HarnessCapabilities,
    TextDelta,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)
from .claude_sdk import ClaudeSdkHarness

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


def _mop_config() -> MopConfig:
    rules_dir_env = os.environ.get("MOP_RULES_DIR")
    backend = os.environ.get("MOP_LLM_BACKEND", "stub")
    return MopConfig(
        rules_dir=Path(rules_dir_env) if rules_dir_env else MopConfig.__dataclass_fields__["rules_dir"].default_factory(),
        llm_backend=backend,  # type: ignore[arg-type]
    )


def _log_violation(session_key: str, rule: str | None, on_violation: str, text: str) -> None:
    log_path = os.environ.get("MOP_LOG_PATH")
    entry = {
        "session_key": session_key,
        "rule": rule,
        "on_violation": on_violation,
        "text_preview": text[:200],
    }
    logger.info("MOP violation: %s", json.dumps(entry))
    if log_path:
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")


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

    @staticmethod
    def _mode() -> str:
        return os.environ.get("MOP_MODE", "audit")

    async def run_turn(self, req: TurnRequest):
        mode = self._mode()

        if mode == "passthrough":
            async for event in self._inner.run_turn(req):
                yield event
            return

        config = _mop_config()
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

            # Audit mode: fire-and-forget, always deliver
            if mode == "audit":
                for event in buffered:
                    yield event
                yield final

                def _audit(sk=req.session_key, text=final.raw_text, cfg=config):
                    import asyncio

                    async def _run():
                        verdict = await evaluate(text, cfg)
                        if verdict.action != Action.ACCEPT:
                            _log_violation(sk, verdict.rule, verdict.action.value, text)

                    asyncio.run(_run())

                threading.Thread(target=_audit, daemon=True).start()
                return

            # Enforce mode: synchronous eval before delivery
            verdict = await evaluate(final.raw_text, config)

            if verdict.action == Action.ACCEPT:
                for event in buffered:
                    yield event
                yield final
                return

            _log_violation(req.session_key, verdict.rule, verdict.action.value, final.raw_text)

            if verdict.action == Action.EDIT:
                rewritten = await mop_rewrite(
                    final.raw_text, verdict.rule or "unknown", verdict.guidance or ""
                )
                for event in buffered:
                    yield event
                yield TurnFinal(
                    session_id=final.session_id,
                    num_turns=final.num_turns,
                    total_cost_usd=final.total_cost_usd,
                    raw_text=rewritten,
                )
                return

            # Reject: discard buffered events, inject guidance, retry
            if attempt < max_retries - 1:
                guidance = verdict.guidance or ""
                inject_prompt = (
                    f"[MOP feedback — attempt {attempt + 1}/{max_retries}] "
                    f"Your previous response violated rule '{verdict.rule}'. "
                    f"{guidance} "
                    f"Revise and try again."
                )
                logger.info(
                    "MOP reject: session=%s rule=%s attempt=%d/%d",
                    req.session_key, verdict.rule, attempt + 1, max_retries,
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
                continue

            logger.warning(
                "MOP max retries exhausted: session=%s rule=%s — delivering despite violation",
                req.session_key, verdict.rule,
            )
            for event in buffered:
                yield event
            yield final
            return

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

    def build_options(
        self,
        *,
        bot,
        chat_id: int,
        thread_id: int | None,
        main_loop,
        rules_dir: Path | None = None,
    ) -> tuple[ClaudeAgentOptions, MOP]:
        """Construct ClaudeAgentOptions wired with the in-process MOP.

        Returns (options, mop_instance). The caller (run_claude) keeps the
        mop_instance alive for the duration of the SDK client's session
        so the Stop hook callback can read its state.

        `main_loop` is the bridge's primary asyncio loop (where the bot's
        httpx client was created). Required because run_claude wraps this
        in `asyncio.run(...)` from a thread-pool worker — calling
        `bot.send_message` directly on that throwaway loop poisons the
        bot's connection pool. See `mop_deliver.py` for the rationale.
        """
        rules = load_rules(rules_dir) if rules_dir else []

        deliver = build_telegram_deliver(
            bot=bot, chat_id=chat_id, thread_id=thread_id, main_loop=main_loop
        )
        evaluator = build_haiku_evaluator(rules=rules)

        mop = MOP(rules=rules, evaluator=evaluator, deliver=deliver)

        mcp_server = build_mcp_server(mop)

        async def stop_hook_callback(input_payload, tool_use_id, context):
            gate = mop_stop(mop)
            if isinstance(gate, Block):
                return {
                    "decision": "block",
                    "reason": gate.reason,
                }
            return {}

        options = ClaudeAgentOptions(
            mcp_servers={"mop": mcp_server},
            allowed_tools=[
                "mcp__mop__submit_message",
                "mcp__mop__submit_justification",
                "mcp__mop__get_rules",
                "mcp__mop__get_status",
            ],
            hooks={
                "Stop": [HookMatcher(hooks=[stop_hook_callback])],
            },
            system_prompt=protocol_prompt(rules),
        )
        return options, mop
