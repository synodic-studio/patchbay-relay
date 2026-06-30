"""Heartbeat toggle command: /heartbeat.

Enables or disables the edit-in-place ⏳ Working — N min bubble that appears
after a long turn. State is stored per-chat in chat_projects.json.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import bridge
from patchbay.projects import get_chat_heartbeat, set_chat_heartbeat
from patchbay.sessions import _session_key


async def cmd_heartbeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show and toggle the heartbeat bubble for this chat."""
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    current = get_chat_heartbeat(key)
    status = "on" if current else "off"
    toggle_label = "Turn off" if current else "Turn on"
    toggle_data = f"heartbeat:{'off' if current else 'on'}"

    buttons = [[InlineKeyboardButton(toggle_label, callback_data=toggle_data)]]
    await update.message.reply_text(
        f"Heartbeat bubble: {status}\nSends ⏳ Working — N min after a long turn and edits it in place.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_heartbeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle heartbeat toggle inline keyboard callback."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    thread_id = query.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    choice = query.data.split(":", 1)[1]  # "on" or "off"
    enabled = choice == "on"
    set_chat_heartbeat(key, enabled)
    bridge.logger.info("Heartbeat set to %s for %s", choice, key)

    status = "on" if enabled else "off"
    toggle_label = "Turn off" if enabled else "Turn on"
    toggle_data = f"heartbeat:{'off' if enabled else 'on'}"
    buttons = [[InlineKeyboardButton(toggle_label, callback_data=toggle_data)]]
    await query.edit_message_text(
        f"Heartbeat bubble: {status}\nSends ⏳ Working — N min after a long turn and edits it in place.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
