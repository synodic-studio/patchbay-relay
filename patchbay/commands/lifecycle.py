"""Lifecycle commands: /start, /clearnew, /cancel, /kill, /restart, /ping.

Each handler does session-state plumbing — clearing, cancelling, draining,
or reporting active turns. Heavy use of bridge module attributes via
`bridge.X` so test patches against bridge see through.
"""

from __future__ import annotations

import json
import os
import time

from telegram import Update
from telegram.ext import ContextTypes

import bridge


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Patchbay active.\nYour Telegram user ID: {uid}\n\n"
        "Commands:\n"
        "/clearnew - Start a fresh conversation (in current topic)\n"
        "/setproject <path> - Set project dir (relative to ~/Developer)\n"
        "/setproject - Clear project binding (use default)\n"
        "/project - Show current project dir\n"
        "/model - Set model (opus/sonnet/haiku) or prefix with !s !o !h\n"
        "/effort - Set effort level (low/medium/high/xhigh/max)\n"
        "/harness - Show or set the agent backend (cc-cli/cc-sdk)\n"
        "/remote-control - Start claude remote-control in this topic's project dir\n"
        "/remote-control stop - Stop remote-control\n"
        "/kill - Kill active Claude process\n"
        "/restart - Restart the bridge\n"
        "/ping - Check if bridge is alive\n"
        "/health - Disk, queues, uptime, counts\n"
        "/activity [event] [count] - Recent activity.jsonl entries (e.g. /activity self_heal)\n"
        "/soak [since] [session] - Compare harness backends (e.g. /soak 7d)\n"
        "/context - Show context-window usage for this chat (cc-sdk only)\n"
        "/compact [steering] - Compact the running context (cc-sdk only)\n"
        "/usage - Show Claude Code quota (tokens + block time remaining)\n\n"
        "Each forum topic runs as an independent Claude session."
    )


async def cmd_clearnew(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = bridge._session_key(chat_id, thread_id)
    bridge.clear_session(key)
    harness = bridge.get_chat_harness(key) or bridge.DEFAULT_HARNESS
    model = bridge.get_chat_model(key) or bridge.DEFAULT_MODEL
    effort = bridge.get_chat_effort(key) or bridge.DEFAULT_EFFORT
    await update.message.reply_text(
        f"Fresh session started.\nHarness: {harness}\nModel: {model}\nEffort: {effort}"
    )
    bridge.logger.info("Session cleared for %s", key)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Soft interrupt — SIGINT for cc-cli, task-cancel for cc-sdk.

    Gives Claude a chance to finish cleanly. Use /kill if this doesn't work.
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = bridge._session_key(chat_id, thread_id)

    state = bridge._sessions.get(key)
    if state is None or (state.proc is None and state.harness is None):
        await update.message.reply_text("No active Claude process in this chat.")
        return

    proc = state.proc
    pid = proc.pid if proc is not None else -1
    harness_name = getattr(state.harness, "name", "cc-cli")

    await bridge._interrupt_session_async(state)
    await bridge._release_processing(state)
    await update.message.reply_text(
        "Interrupted. Session preserved — next message resumes."
    )
    bridge.logger.info(
        "User %d cancelled Claude turn for %s (harness=%s, pid=%d)",
        user_id, key, harness_name, pid,
    )
    bridge._log_activity(
        "process_cancel",
        session_key=key,
        pid=pid,
        user_id=user_id,
        reason="manual",
        harness=harness_name,
    )


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hard kill — SIGKILL for cc-cli, task-cancel for cc-sdk.

    Backend-agnostic via `_cancel_session_async`. Use /cancel first for a
    graceful interrupt; /kill when that doesn't work.
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = bridge._session_key(chat_id, thread_id)

    state = bridge._sessions.get(key)
    if state is None or (state.proc is None and state.harness is None):
        await update.message.reply_text("No active Claude process in this chat.")
        return

    # Capture pid before cancel for logging — cc-sdk has no proc, log -1.
    proc = state.proc
    pid = proc.pid if proc is not None else -1
    harness_name = getattr(state.harness, "name", "cc-cli")

    await bridge._cancel_session_async(state)
    await bridge._release_processing(state)
    await update.message.reply_text(
        "Killed active Claude process. Session preserved — next message resumes."
    )
    bridge.logger.info(
        "User %d killed Claude turn for %s (harness=%s, pid=%d)",
        user_id,
        key,
        harness_name,
        pid,
    )
    bridge._log_activity(
        "process_kill",
        session_key=key,
        pid=pid,
        user_id=user_id,
        reason="manual",
        harness=harness_name,
    )


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart the bridge process. Launchd will respawn it.

    Two modes:
      * `/restart` (default) — drain mode. Block new messages, wait for
        in-flight turns to finish (up to RESTART_DRAIN_TIMEOUT), then exit.
        Preserves work in progress.
      * `/restart force` — old behavior. SIGTERM everything immediately
        and exit. For when the bridge itself is wedged and waiting won't
        help.
    """
    user_id = update.effective_user.id
    parts = (update.message.text or "").split()
    force = len(parts) > 1 and parts[1].lower() in ("force", "kill", "now")

    active = bridge._iter_active_sessions()
    n_active = len(active)

    if force:
        await update.message.reply_text(
            f"Restarting (force) — terminating {n_active} active turn{'s' if n_active != 1 else ''}..."
            if n_active
            else "Restarting (force)..."
        )
        bridge.logger.info(
            "User %d triggered FORCE bridge restart (%d active)", user_id, n_active
        )
    elif n_active == 0:
        await update.message.reply_text("Restarting (no active turns)...")
        bridge.logger.info("User %d triggered bridge restart (no active turns)", user_id)
    else:
        await update.message.reply_text(
            f"Restarting — draining {n_active} active turn{'s' if n_active != 1 else ''} "
            f"(up to {bridge.RESTART_DRAIN_TIMEOUT // 60} min). "
            f"Use /restart force to skip."
        )
        bridge.logger.info(
            "User %d triggered bridge restart (drain mode, %d active, timeout %ds)",
            user_id,
            n_active,
            bridge.RESTART_DRAIN_TIMEOUT,
        )

    bridge.RESTART_NOTIFY_FILE.write_text(
        json.dumps(
            {
                "chat_id": update.effective_chat.id,
                "thread_id": update.message.message_thread_id,
            }
        )
    )

    # Block new incoming messages while we drain / kill.
    bridge._shutting_down = True

    if not force and n_active > 0:
        await bridge._drain_active_turns(deadline_seconds=bridge.RESTART_DRAIN_TIMEOUT)

    # Force-terminate anything still running (drain timed out, or user used force).
    for key, proc in bridge._iter_active_procs():
        if proc.poll() is None:
            proc.terminate()
            bridge.logger.info(
                "Terminated Claude process for %s (pid %d)", key, proc.pid
            )

    for key, state in bridge._iter_active_sessions():
        if state.proc is not None:
            continue  # already terminated above
        try:
            await bridge._cancel_session_async(state)
        except Exception:  # noqa: BLE001
            bridge.logger.exception(
                "Cancel for cc-sdk session %s during restart raised", key
            )

    if bridge._remote_proc and bridge._remote_proc.poll() is None:
        bridge._remote_proc.terminate()
        bridge.logger.info(
            "Terminated remote-control process (pid %d)", bridge._remote_proc.pid
        )

    os._exit(1)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not any(s.processing for s in bridge._sessions.values()):
        await update.message.reply_text("pong — no active sessions")
        return
    now = time.time()
    lines = ["pong — active sessions:"]
    for key in sorted(k for k, s in bridge._sessions.items() if s.processing):
        started = bridge._sessions[key].started_at if key in bridge._sessions else None
        label = bridge._session_display_label(key)
        if started:
            elapsed = int(now - started)
            mins, secs = divmod(elapsed, 60)
            lines.append(f"  {label}: running {mins}m{secs:02d}s")
        else:
            lines.append(f"  {label}: running (start time unknown)")
    await update.message.reply_text("\n".join(lines))
