"""Telegram delivery closure for MOP v2.

build_telegram_deliver(bot, chat_id, thread_id) returns a Deliver-shaped
async callable: `(text, system_note) -> None`. The closure body is the
actual `bot.send_message` call. MOP calls this from `_apply` on
accept/rewrite, and from `_failed_open` with a non-None system_note.

When system_note is set, we send TWO Telegram messages: the user message
unchanged, then a separate bubble with a clear marker prefix so the user
sees the rule-bypass warning as its own thing.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable


def build_telegram_deliver(
    *, bot: Any, chat_id: int, thread_id: int | None
) -> Callable[[str, str | None], Awaitable[None]]:
    base_kwargs: dict[str, Any] = {"chat_id": chat_id}
    if thread_id is not None:
        base_kwargs["message_thread_id"] = thread_id

    async def deliver(text: str, system_note: str | None = None) -> None:
        await bot.send_message(text=text, **base_kwargs)
        if system_note:
            await bot.send_message(
                text=f"⚠️ {system_note}",
                **base_kwargs,
            )

    return deliver
