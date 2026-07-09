"""Context window commands: /context, /compact.

These commands run a one-shot inquiry against the chat's active session
to read or compact its context window. Pi uses the two-turn fallback
(summarize → clear → handoff) since it doesn't expose a native compact.
"""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

import bridge
from patchbay.config import QUOTA_HIT_PREFIX
from patchbay.sessions import _session_key, clear_session


_SUMMARIZE_PROMPT = (
    "PATCHBAY COMPACT — produce a handoff summary of our conversation so "
    "far. Output ONLY the summary text, no preamble, no closing remark, "
    "no markdown wrapping. Cover: (1) the original goal and any sub-goals, "
    "(2) decisions made and why, (3) work in progress / what's next, "
    "(4) key file paths, commands, and gotchas, (5) anything I asked you "
    "to remember. Be thorough — this summary is the only memory carried "
    "into the next session. Override any standing 'be brief' instruction "
    "for THIS message only; handoff summaries must be complete."
)


def _fmt_tokens(n: int) -> str:
    """Render token counts as '12.3k' / '1.0M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _resolve_harness_for_inquiry(session_key: str):
    """Back-compat shim; the implementation now lives in commands.inquiry."""
    from patchbay.commands.inquiry import resolve_harness_for_inquiry

    return resolve_harness_for_inquiry(session_key)


def _build_handoff_prompt(summary: str) -> str:
    return (
        "[Carrying context forward from a compacted prior session.]\n\n"
        f"{summary.strip()}\n\n"
        "[End of carried-forward summary. Acknowledge briefly so we can "
        "continue from here.]"
    )


async def _fallback_compact(
    *,
    update: Update,
    session_key: str,
    instructions: str | None,
) -> None:
    """Generic /compact for harnesses that don't have a native one.

    Two-turn flow on the existing run_claude path:
      1. Run a synthesizer turn against the current session asking for a
         handoff summary.
      2. Clear the chat's session id (same as /clearnew).
      3. Run a handoff turn whose prompt IS the summary, in the new
         (now empty) session. The agent acknowledges; the new session
         carries the compacted context forward.
    """
    loop = asyncio.get_running_loop()
    summarize_prompt = _SUMMARIZE_PROMPT
    if instructions:
        summarize_prompt += f"\n\nADDITIONAL FOCUS FROM USER: {instructions}"

    await update.message.reply_text("Compacting (fallback): summarizing prior session…")
    try:
        summary = await loop.run_in_executor(
            bridge._executor, bridge.run_claude, summarize_prompt, session_key
        )
    except Exception as exc:  # noqa: BLE001
        bridge.logger.exception(
            "/compact fallback summarize failed for %s", session_key
        )
        await update.message.reply_text(f"Compact failed during summarize: {exc}")
        return

    if not summary or summary.startswith(QUOTA_HIT_PREFIX):
        await update.message.reply_text(
            "Compact aborted — couldn't get a summary. Try again later."
        )
        return

    summary = summary.strip()
    summary_words = len(summary.split())

    clear_session(session_key)
    bridge.logger.info(
        "Compact fallback for %s: cleared session, summary=%d words",
        session_key,
        summary_words,
    )

    handoff_prompt = _build_handoff_prompt(summary)
    await update.message.reply_text(
        f"Started fresh session. Handing forward summary ({summary_words} words)…"
    )
    try:
        ack = await loop.run_in_executor(
            bridge._executor, bridge.run_claude, handoff_prompt, session_key
        )
    except Exception as exc:  # noqa: BLE001
        bridge.logger.exception(
            "/compact fallback handoff failed for %s", session_key
        )
        await update.message.reply_text(
            f"Summary captured but handoff failed: {exc}\n\n"
            "Your next message will start a fresh session without context."
        )
        return

    await update.message.reply_text(f"Compact done. Agent ack:\n\n{ack[:1500]}")


async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current context-window usage for this chat's session.

    Dispatches to the topic's active harness: a harness that implements
    ContextQueryCapableHarness reports usage, otherwise we say so. pi reports
    real token usage from its session transcript, estimating when a provider
    didn't record it.
    """
    from patchbay.harness import ContextQueryCapableHarness

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    harness_name, harness, req = _resolve_harness_for_inquiry(key)

    if not isinstance(harness, ContextQueryCapableHarness):
        await update.message.reply_text(
            f"/context isn't supported on {harness_name}. Use /compact instead."
        )
        return

    if not req.resume_session_id:
        await update.message.reply_text(
            "No active session yet — send a message first to start one."
        )
        return

    try:
        cu = await harness.get_context(req)
    except Exception as exc:  # noqa: BLE001
        bridge.logger.exception("/context query failed for %s", key)
        await update.message.reply_text(f"context check failed: {exc}")
        return

    await update.message.reply_text(
        f"Context: {_fmt_tokens(cu.used_tokens)} / {_fmt_tokens(cu.max_tokens)} "
        f"({cu.percentage:.0f}%)"
    )


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Compact the running context. Optional steering text after the command.

    Uses the two-turn fallback (summarize → clear → handoff) since
    pi doesn't have a native /compact command.
    """
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    parts = (update.message.text or "").split(maxsplit=1)
    instructions = parts[1].strip() if len(parts) > 1 else None

    resolved = _resolve_harness_for_inquiry(key)
    if resolved is None:
        await update.message.reply_text("No harness configured for this chat.")
        return
    harness_name, harness, req = resolved

    if not req.resume_session_id:
        await update.message.reply_text(
            "Nothing to compact — no active session in this chat. "
            "Send a message first to start one."
        )
        return

    await _fallback_compact(update=update, session_key=key, instructions=instructions)
