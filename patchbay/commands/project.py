"""Project / model / effort / harness / remote-control commands.

Pure data lookups (config, projects, models, efforts, sessions, activity
log) come from their canonical patchbay submodules. Genuinely bridge-owned
runtime state (the executor, the remote-control proc handle, the
remote-stdout drainer, `bridge.logger`) is still reached via `bridge.X`
since it lives nowhere else.

Module-load order: bridge.py only imports patchbay.commands inside its
module body via re-export at the end, so `import bridge` here doesn't form
a load-time cycle when a caller imports bridge first.
"""

from __future__ import annotations

import asyncio
import os
import select
import subprocess
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import bridge
from patchbay import config as _config
from patchbay.activity import log_activity
from patchbay.config import (
    ANSI_RE,
    CLAUDE_PATH,
    DEFAULT_HARNESS,
    VALID_HARNESSES,
)
from patchbay.efforts import (
    DEFAULT_EFFORT,
    VALID_EFFORTS,
    get_chat_effort,
    set_chat_effort,
)
from patchbay.harness import codex_models
from patchbay.models import (
    VALID_MODELS,
    default_model_label,
    get_chat_model,
    model_is_valid,
    set_chat_model,
)
from patchbay.projects import (
    _load_chat_projects,
    _parse_project_entry,
    get_all_projects,
    get_chat_agent,
    get_chat_harness,
    get_chat_working_dir,
    set_chat_harness,
    set_chat_project,
)
from patchbay.sessions import _session_key, clear_session


async def cmd_setproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    args = context.args
    if not args:
        projects = get_all_projects()
        buttons = [[InlineKeyboardButton(name, callback_data=f"setproject:{name}")] for name in projects]
        buttons.append([InlineKeyboardButton("Clear (use ~/Developer)", callback_data="setproject:__clear__")])
        await update.message.reply_text(
            "Pick a project (A-Z).\nOr type: /setproject <path>",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    rel_path = args[0]
    # Path traversal prevention
    if ".." in rel_path or rel_path.startswith("/") or "\\" in rel_path:
        await update.message.reply_text("Invalid project path.")
        return
    abs_path = os.path.join(_config.WORKING_DIR, rel_path)
    real_path = os.path.realpath(abs_path)
    if not real_path.startswith(os.path.realpath(_config.WORKING_DIR)):
        await update.message.reply_text("Invalid project path.")
        return
    if not os.path.isdir(abs_path):
        await update.message.reply_text(f"Directory not found: ~/Developer/{rel_path}")
        return

    set_chat_project(key, rel_path)
    clear_session(key)
    chat_title = update.effective_chat.title or "DM"
    await update.message.reply_text(
        f"Project set: ~/Developer/{rel_path}\nChat: {chat_title}\nSession reset. Agent will run from this directory."
    )
    bridge.logger.info("Project set to %s for %s (%s)", rel_path, key, chat_title)


async def callback_setproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for project selection."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    thread_id = query.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    data = query.data  # "setproject:<name>" or "setproject:__clear__"
    rel_path = data.split(":", 1)[1]

    if rel_path == "__clear__":
        set_chat_project(key, None)
        clear_session(key)
        await query.edit_message_text("Project cleared. Using default: ~/Developer\nSession reset.")
        bridge.logger.info("Project cleared for %s", key)
        return

    # Path traversal prevention
    if ".." in rel_path or rel_path.startswith("/") or "\\" in rel_path:
        await query.edit_message_text("Invalid project path.")
        return

    abs_path = os.path.join(_config.WORKING_DIR, rel_path)
    real_path = os.path.realpath(abs_path)
    if not real_path.startswith(os.path.realpath(_config.WORKING_DIR)):
        await query.edit_message_text("Invalid project path.")
        return
    if not os.path.isdir(abs_path):
        await query.edit_message_text(f"Directory not found: ~/Developer/{rel_path}")
        return

    set_chat_project(key, rel_path)
    clear_session(key)
    chat_title = update.effective_chat.title or "DM"
    await query.edit_message_text(
        f"Project set: ~/Developer/{rel_path}\nChat: {chat_title}\nSession reset. Agent will run from this directory."
    )
    bridge.logger.info("Project set to %s for %s (%s)", rel_path, key, chat_title)


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)
    agent = get_chat_agent(key)
    rel_path_entry, _ = _parse_project_entry(_load_chat_projects().get(key))
    if rel_path_entry:
        label = f"Project: ~/Developer/{rel_path_entry}"
        if agent:
            label += f" (agent: {agent})"
        await update.message.reply_text(label)
    else:
        await update.message.reply_text("No project set. Using default: ~/Developer")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or show the model for this chat/topic.

    Pi uses its dynamic picker; Codex accepts explicit model IDs and delegates
    its default to CODEX_MODEL or the CLI configuration.
    """
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    harness = get_chat_harness(key) or DEFAULT_HARNESS
    default_label = default_model_label(harness)
    args = context.args
    if args:
        choice = args[0].lower()
        if choice == "default":
            set_chat_model(key, None, harness=harness)
            await update.message.reply_text(f"Model reset to default ({default_label}).")
            bridge.logger.info("Model cleared for %s", key)
            return
        try:
            valid = await asyncio.to_thread(model_is_valid, choice, harness=harness)
        except codex_models.CodexModelsUnavailable:
            await update.message.reply_text("Codex models are unavailable right now. Try /model again.")
            return
        if not valid:
            await update.message.reply_text(
                (
                    "Invalid Codex model. Use /model to pick an available model."
                    if harness == "codex"
                    else f"Invalid model. Pick from: {', '.join(VALID_MODELS)}, default"
                )
            )
            return
        set_chat_model(key, choice, harness=harness)
        await update.message.reply_text(f"Model set to {choice}. Takes effect on next message.")
        bridge.logger.info("Model set to %s for %s", choice, key)
        return

    current = get_chat_model(key, harness=harness) or f"default ({default_label})"
    if harness == "codex":
        try:
            available = await asyncio.to_thread(codex_models.list_codex_models)
        except codex_models.CodexModelsUnavailable:
            await update.message.reply_text("Codex models are unavailable right now. Try /model again.")
            return
        heading = f"Codex model: {current}\nPick a model (/model):"
    else:
        from patchbay.models import list_available_models

        available = [(name, name) for name, _ in list_available_models()]
        heading = f"Current model: {current}\nPick a model (or prefix with !<shortcut> for one-shot):"

    buttons = []
    row = []
    for name, label in available:
        row.append(InlineKeyboardButton(label, callback_data=f"model:{harness}:{name}"))
        if len(row) >= 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("default", callback_data=f"model:{harness}:__default__")])
    await update.message.reply_text(heading, reply_markup=InlineKeyboardMarkup(buttons))


async def callback_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for model selection."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    thread_id = query.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    harness = get_chat_harness(key) or DEFAULT_HARNESS
    default_label = default_model_label(harness)
    payload = query.data.split(":", 1)[1]
    if ":" in payload:
        menu_harness, choice = payload.split(":", 1)
    else:
        # Model menus predating harness binding were generated for pi.
        menu_harness, choice = "pi", payload
    if menu_harness != harness:
        await query.edit_message_text("This model menu belongs to another harness. Use /model to open a current menu.")
        return
    if choice == "__default__":
        set_chat_model(key, None, harness=harness)
        await query.edit_message_text(f"Model reset to default ({default_label}).")
        bridge.logger.info("Model cleared for %s", key)
        return
    try:
        valid = await asyncio.to_thread(model_is_valid, choice, harness=harness)
    except codex_models.CodexModelsUnavailable:
        await query.edit_message_text("Codex models are unavailable right now. Try /model again.")
        return
    if not valid:
        await query.edit_message_text(f"Invalid model: {choice}")
        return

    set_chat_model(key, choice, harness=harness)
    await query.edit_message_text(f"Model set to {choice}. Takes effect on next message.")
    bridge.logger.info("Model set to %s for %s", choice, key)


async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or show the effort level for this chat/topic."""
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    args = context.args
    if args:
        choice = args[0].lower()
        if choice == "default":
            set_chat_effort(key, None)
            await update.message.reply_text(f"Effort reset to default ({DEFAULT_EFFORT}).")
            bridge.logger.info("Effort cleared for %s", key)
            return
        if choice not in VALID_EFFORTS:
            await update.message.reply_text(f"Invalid effort. Choose: {', '.join(VALID_EFFORTS)}, default")
            return
        set_chat_effort(key, choice)
        await update.message.reply_text(f"Effort set to {choice}. Takes effect on next message.")
        bridge.logger.info("Effort set to %s for %s", choice, key)
        return

    current = get_chat_effort(key) or f"default ({DEFAULT_EFFORT})"
    buttons = [
        [InlineKeyboardButton(level, callback_data=f"effort:{level}") for level in VALID_EFFORTS],
        [InlineKeyboardButton("default", callback_data="effort:__default__")],
    ]
    await update.message.reply_text(
        f"Current effort: {current}\nPick an effort level:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for effort selection."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    thread_id = query.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    choice = query.data.split(":", 1)[1]
    if choice == "__default__":
        set_chat_effort(key, None)
        await query.edit_message_text(f"Effort reset to default ({DEFAULT_EFFORT}).")
        bridge.logger.info("Effort cleared for %s", key)
        return
    if choice not in VALID_EFFORTS:
        await query.edit_message_text(f"Invalid effort: {choice}")
        return

    set_chat_effort(key, choice)
    await query.edit_message_text(f"Effort set to {choice}. Takes effect on next message.")
    bridge.logger.info("Effort set to %s for %s", choice, key)


async def cmd_harness(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or select this topic's backend, clearing incompatible sessions."""
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)
    current = get_chat_harness(key) or DEFAULT_HARNESS

    args = context.args
    if args:
        choice = args[0].lower()
        if choice != "default" and choice not in VALID_HARNESSES:
            await update.message.reply_text(f"Invalid harness. Choose: {', '.join(VALID_HARNESSES)}, default")
            return
        selected = DEFAULT_HARNESS if choice == "default" else choice
        set_chat_harness(key, None if choice == "default" else choice)
        switched = selected != current
        if switched:
            clear_session(key)
        label = (
            f"Harness reset to default ({selected})."
            if choice == "default"
            else f"Harness set to {selected}. Takes effect on next message."
        )
        if switched:
            label += " Session reset."
        await update.message.reply_text(label)
        bridge.logger.info("Harness set to %s for %s", selected, key)
        return

    await update.message.reply_text(
        f"Harness: {current}\nAvailable: {', '.join(VALID_HARNESSES)}\nUse /harness <name> to switch."
    )


async def cmd_remote_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start or stop claude remote-control in this topic's project dir."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    args = (update.message.text or "").split()
    if len(args) > 1 and args[1].lower() == "stop":
        if bridge._remote_proc and bridge._remote_proc.poll() is None:
            bridge._remote_proc.terminate()
            bridge._remote_proc.wait(timeout=5)
            cwd_label = get_chat_working_dir(bridge._remote_proc_key or key)
            bridge._remote_proc = None
            bridge._remote_proc_key = None
            await update.message.reply_text(f"Remote control stopped ({cwd_label})")
            bridge.logger.info("User %d stopped remote-control", user_id)
        else:
            bridge._remote_proc = None
            bridge._remote_proc_key = None
            await update.message.reply_text("No remote-control process running.")
        return

    if bridge._remote_proc and bridge._remote_proc.poll() is None:
        bridge._remote_proc.terminate()
        try:
            bridge._remote_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bridge._remote_proc.kill()
        bridge.logger.info("Replaced existing remote-control process (pid %d)", bridge._remote_proc.pid)

    chat_cwd = get_chat_working_dir(key)
    await update.message.reply_text(f"Starting remote-control in {chat_cwd}...")

    proc = subprocess.Popen(
        [CLAUDE_PATH, "remote-control"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=chat_cwd,
    )
    bridge._remote_proc = proc
    bridge._remote_proc_key = key
    bridge.logger.info("Started claude remote-control (pid %d) in %s", proc.pid, chat_cwd)
    log_activity("remote_control_start", session_key=key, cwd=chat_cwd, pid=proc.pid)

    loop = asyncio.get_running_loop()

    def _read_initial_output() -> list[str]:
        collected = []
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                remaining = proc.stdout.read()
                if remaining:
                    collected.extend(remaining.splitlines())
                break
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if line:
                    collected.append(line.rstrip())
        seen: set[str] = set()
        result: list[str] = []
        for raw in collected:
            clean = ANSI_RE.sub("", raw).strip()
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
        return result

    lines = await loop.run_in_executor(bridge._executor, _read_initial_output)

    if proc.poll() is not None:
        output = "\n".join(lines) if lines else "(no output)"
        await update.message.reply_text(f"Remote control exited (code {proc.returncode}):\n{output}")
        bridge._remote_proc = None
        bridge._remote_proc_key = None
    else:
        # Spawn the background drainer now: nobody is reading stdout from
        # here on, and remote-control would deadlock on a full pipe.
        bridge._start_remote_drain(proc)
        output = "\n".join(lines) if lines else "(waiting for connection info...)"
        await update.message.reply_text(
            f"Remote control running (pid {proc.pid}):\n{output}\n\nUse /remote stop to shut it down."
        )
