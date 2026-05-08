"""ClaudeSdkMopHarness — cc-sdk wired with in-process Model Output Protocol.

The v2 dispatch in `bridge.run_claude` calls `build_options()` to get a
`ClaudeAgentOptions` that:

  * Mounts MOP as an in-process MCP server (`mcp__mop__submit_message` etc.).
    Model output is delivered via that tool — never via TextBlock streaming —
    which keeps the bridge from double-sending.
  * Registers a `Stop` hook callback that asks the MOP instance whether the
    turn is allowed to terminate. If MOP returns `Block`, the SDK retries
    with the supplied reason; otherwise the turn ends cleanly.
  * Includes a system prompt enumerating the active rules.

The bridge holds the returned MOP instance on `SessionState.mop` for the
lifetime of the SDK client so the Stop-hook closure stays alive.

The legacy buffer-then-evaluate `run_turn` path (passthrough/audit/enforce
modes, retry loop, edit/rewrite) was removed in T15 — v2 evaluates inline
via `mop.filter()` inside the MCP tool handler.
"""

from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from mop import MOP, build_haiku_evaluator, protocol_prompt
from mop import stop as mop_stop
from mop.rules import load_rules
from mop.types import Block

from ..mop_deliver import build_telegram_deliver
from ..mop_verbose import build_server as build_mop_mcp_server
from ..mop_verbose import is_verbose_enabled
from .base import HarnessCapabilities

logger = logging.getLogger(__name__)

_CAPABILITIES = HarnessCapabilities(
    supports_resume=True,
    supports_tool_streaming=True,
    supports_interrupt=True,
    supports_effort=True,
    supports_mcp=True,
    # MOP delivers via MCP tool calls, not streamed TextBlocks. Inflight push
    # would require a separate streaming-eval path.
    supports_inflight_push=False,
    # /context and /compact require a regular ClaudeSdkHarness instance —
    # the v2 dispatch doesn't construct one. Users hit /context on cc-sdk-mop
    # and the bridge tells them to switch to cc-sdk for that command.
    supports_context_query=False,
    supports_compact=False,
)


class ClaudeSdkMopHarness:
    """cc-sdk wired with in-process MOP for output policing.

    This class is a thin holder for `name`, `capabilities`, and
    `build_options()`. It has no `run_turn` because the v2 dispatch in
    `bridge.run_claude` drives `ClaudeSDKClient` directly with the options
    `build_options()` returns — MOP intercepts model output via its MCP
    tools rather than via the harness event stream.
    """

    name = "cc-sdk-mop"
    capabilities = _CAPABILITIES

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
        # Pin the deliver closure on the MOP instance so the bridge can read
        # deliver.delivery_count to detect "MOP delivered nothing this turn"
        # and trigger the plain-text fallback safety net.
        mop._patchbay_deliver = deliver  # type: ignore[attr-defined]

        verbose = is_verbose_enabled()
        if verbose:
            logger.info("mop.verbose ENABLED for chat=%s — every verdict will surface", chat_id)
        mcp_server = build_mop_mcp_server(
            mop,
            bot=bot,
            chat_id=chat_id,
            thread_id=thread_id,
            main_loop=main_loop,
            enabled=verbose,
        )

        async def stop_hook_callback(input_payload, tool_use_id, context):
            stop_active = bool(input_payload.get("stop_hook_active"))
            sent = mop.sent_message_this_turn
            gate = mop_stop(mop)
            if isinstance(gate, Block):
                logger.info(
                    "mop.stop_hook BLOCK chat=%s sent_this_turn=%s stop_hook_active=%s reason=%r",
                    chat_id, sent, stop_active, gate.reason,
                )
                return {"decision": "block", "reason": gate.reason}
            logger.info(
                "mop.stop_hook ALLOW chat=%s sent_this_turn=%s stop_hook_active=%s",
                chat_id, sent, stop_active,
            )
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
