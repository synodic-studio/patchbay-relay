"""Context window commands: /context, /compact.

These commands run a one-shot inquiry against the chat's active session
to read or compact its context window. cc-sdk has native support; every
other harness uses the two-turn fallback (summarize → clear → handoff).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

import bridge
from patchbay.config import CLAUDE_PATH, MAX_TIMEOUT, QUOTA_HIT_PREFIX
from patchbay.efforts import resolve_effort
from patchbay.harness import (
    CAPABILITIES_BY_NAME,
    ClaudeCliHarness,
    ClaudeSdkHarness,
    ClaudeSdkMopHarness,
    TurnRequest,
)
from patchbay.models import get_chat_model
from patchbay.projects import get_chat_harness, get_chat_working_dir
from patchbay.sessions import _session_key, clear_session, get_session_id


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
    """Build a harness instance + TurnRequest suitable for one-shot inquiry
    methods (get_context, compact). Mirrors the dispatch in run_claude
    minus the proc-mirroring and per-turn callbacks. Returns
    (harness_name, harness, req) or None if the chat's harness doesn't
    exist or isn't suitable.
    """
    from patchbay.config import DEFAULT_HARNESS, VALID_HARNESSES

    chat_cwd = get_chat_working_dir(session_key)
    session_id = get_session_id(session_key)
    model = get_chat_model(session_key)
    effort = resolve_effort(session_key)

    harness_name = get_chat_harness(session_key) or DEFAULT_HARNESS
    if harness_name not in VALID_HARNESSES:
        harness_name = DEFAULT_HARNESS

    if harness_name == "cc-sdk":
        harness = ClaudeSdkHarness(
            cli_path=CLAUDE_PATH, max_timeout_seconds=MAX_TIMEOUT
        )
    elif harness_name == "cc-cli":
        harness = ClaudeCliHarness(
            claude_path=CLAUDE_PATH, max_timeout_seconds=MAX_TIMEOUT
        )
    elif harness_name == "cc-sdk-mop":
        # cc-sdk-mop has no run_turn/get_context/compact — its v2 dispatch
        # lives in bridge.run_claude. Returning the instance here lets
        # cmd_context render a "not supported on cc-sdk-mop" hint and lets
        # cmd_compact fall through to the run_claude-based fallback path.
        harness = ClaudeSdkMopHarness()
    elif harness_name == "pi":
        from patchbay.harness import PiHarness

        harness = PiHarness(max_timeout_seconds=MAX_TIMEOUT)
    else:
        return None

    req = TurnRequest(
        prompt="",  # /context and /compact set their own prompt
        session_key=session_key,
        project_dir=Path(chat_cwd),
        system_prompt="",
        resume_session_id=session_id,
        model=model,
        effort=effort,
        allowed_tools=None,
        disallowed_tools=None,
        max_turns=None,
        plugin_dir=None,
    )
    return harness_name, harness, req


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

    cc-sdk only today (cc-cli/pi advertise supports_context_query=False).
    For unsupported harnesses, suggest /harness cc-sdk.
    """
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    resolved = _resolve_harness_for_inquiry(key)
    if resolved is None:
        await update.message.reply_text("No harness configured for this chat.")
        return
    harness_name, harness, req = resolved

    caps = CAPABILITIES_BY_NAME.get(harness_name)
    if caps is None or not caps.supports_context_query:
        await update.message.reply_text(
            f"/context isn't supported on {harness_name} yet. "
            f"Try /harness cc-sdk for this chat."
        )
        return

    try:
        usage = await harness.get_context(req)
    except Exception as exc:  # noqa: BLE001
        bridge.logger.exception("/context failed for %s", key)
        await update.message.reply_text(f"Couldn't read context: {exc}")
        return

    used = _fmt_tokens(usage.used_tokens)
    cap = _fmt_tokens(usage.max_tokens)
    pct = f"{usage.percentage:.0f}%"
    await update.message.reply_text(f"Context: {used} / {cap} ({pct})")


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Compact the running context. Optional steering text after the command.

    Two paths:
    - Native (cc-sdk): pushes claude's `/compact` slash command into a
      transient SDK client and reports before/after token counts.
    - Fallback (every other harness): runs a summarizer turn, clears the
      session, then runs a handoff turn whose prompt IS the summary.
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

    caps = CAPABILITIES_BY_NAME.get(harness_name)
    if caps is not None and caps.supports_compact:
        # Native path.
        await update.message.reply_text("Compacting context…")
        try:
            result = await harness.compact(req, instructions=instructions)
        except Exception as exc:  # noqa: BLE001
            bridge.logger.exception("/compact native failed for %s", key)
            await update.message.reply_text(f"Compact failed: {exc}")
            return
        await update.message.reply_text(result.message)
        return

    # Fallback path: works on every harness via run_claude.
    await _fallback_compact(update=update, session_key=key, instructions=instructions)
