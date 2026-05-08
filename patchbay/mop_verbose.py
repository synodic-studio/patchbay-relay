"""Verbose-mode MCP server for MOP — surfaces every verdict to the chat.

When enabled (env var `MOP_VERBOSE` truthy), wraps `submit_message` and
`submit_justification` so that *every* verdict — including rejected — is
mirrored to Telegram as a separate diagnostic message. Rejected messages
are normally invisible to the user (MOP withholds them); verbose mode
shows the original text plus the violation reasons so we can debug rule
behavior end-to-end.

When disabled, `build_server` returns MOP's stock `build_mcp_server(mop)`
unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from mop import MOP, build_mcp_server
from mop.mcp import _verdict_payload, build_tool_handlers
from mop.types import Accepted, AcceptedFailedOpen, Rejected, Rewritten

logger = logging.getLogger(__name__)


def is_verbose_enabled() -> bool:
    return os.environ.get("MOP_VERBOSE", "").strip().lower() in {"1", "true", "yes", "on"}


def _format_verdict(original: str, verdict: Any, *, is_justification: bool = False) -> str:
    label = "JUSTIFY" if is_justification else "SUBMIT"
    if isinstance(verdict, Accepted):
        return f"[mop {label} ✓ accepted]"
    if isinstance(verdict, Rewritten):
        return (
            f"[mop {label} ✎ rewritten]\n"
            f"original: {original}\n"
            f"sent:     {verdict.rewritten}"
        )
    if isinstance(verdict, Rejected):
        violations = "\n  - ".join(verdict.violations) if verdict.violations else "(none)"
        return (
            f"[mop {label} ✗ rejected — NOT delivered]\n"
            f"original: {original}\n"
            f"violations:\n  - {violations}"
        )
    if isinstance(verdict, AcceptedFailedOpen):
        return (
            f"[mop {label} ⚠ failed-open] {original}\n"
            f"note: {verdict.system_note}"
        )
    return f"[mop {label} ?] {original} verdict={verdict!r}"


def build_server(
    mop: MOP,
    *,
    bot: Any,
    chat_id: int,
    thread_id: int | None,
    main_loop: asyncio.AbstractEventLoop,
    enabled: bool,
):
    """Return an McpSdkServerConfig. If enabled is False, delegates to MOP's stock server."""
    if not enabled:
        return build_mcp_server(mop)

    handlers = build_tool_handlers(mop)
    base_kwargs: dict[str, Any] = {"chat_id": chat_id}
    if thread_id is not None:
        base_kwargs["message_thread_id"] = thread_id

    async def _diag(text: str) -> None:
        try:
            fut = asyncio.run_coroutine_threadsafe(
                bot.send_message(text=text, **base_kwargs), main_loop
            )
            await asyncio.wrap_future(fut)
        except Exception:
            logger.exception("mop.verbose diag send failed chat=%s", chat_id)

    @tool(
        "submit_message",
        "Submit a message for delivery to the user. Returns one of "
        "{accepted, rewritten, rejected, accepted_failed_open}.",
        {"message": str},
    )
    async def _submit_message(args: dict[str, Any]) -> dict[str, Any]:
        msg = args["message"]
        verdict = await mop.submit_message(msg)
        await _diag(_format_verdict(msg, verdict))
        return _wrap_verdict(verdict)

    @tool(
        "submit_justification",
        "Argue why a previously-rejected message should still be delivered. "
        "Capped at 4 attempts before MOP failed-opens.",
        {"justification": str},
    )
    async def _submit_justification(args: dict[str, Any]) -> dict[str, Any]:
        pending = mop.pending_message
        verdict = await mop.submit_justification(args["justification"])
        await _diag(_format_verdict(pending or "(no pending)", verdict, is_justification=True))
        return _wrap_verdict(verdict)

    @tool(
        "get_rules",
        "List active rules. Optional regex filter matches rule names and guidance.",
        {"filter": str | None},
    )
    async def _get_rules(args: dict[str, Any]) -> dict[str, Any]:
        return await handlers["get_rules"](args)

    @tool(
        "get_status",
        "Return current MOP state.",
        {},
    )
    async def _get_status(args: dict[str, Any]) -> dict[str, Any]:
        return await handlers["get_status"](args)

    return create_sdk_mcp_server(
        name="mop",
        version="1.0.0",
        tools=[_submit_message, _submit_justification, _get_rules, _get_status],
    )


def _wrap_verdict(verdict: Any) -> dict[str, Any]:
    """Same MCP envelope MOP's stock server uses, without re-running submit."""
    return {"content": [{"type": "text", "text": json.dumps(_verdict_payload(verdict))}]}
