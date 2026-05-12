"""Telegram delivery closure for MOP.

build_telegram_deliver(bot, chat_id, thread_id, main_loop) returns a
Deliver-shaped async callable: `(text, system_note) -> None`. The
closure schedules `bot.send_message` on the bridge's main asyncio loop
via `run_coroutine_threadsafe`, because the cc-sdk-mop dispatch invokes
this deliver from inside `asyncio.run(_drive_mop_session())` running in
a thread-pool worker. Calling `bot.send_message` directly on the
temporary loop binds httpx connections to a loop that closes when the
SDK turn ends — that poisons the bot for any subsequent main-loop
request (handle_photo, the next /command, etc.) with `RuntimeError:
Event loop is closed`. Routing through `main_loop` keeps every Telegram
I/O on the loop where the bot's httpx client was created.

When system_note is set, we send TWO Telegram messages: the user message
unchanged, then a separate bubble with a clear marker prefix so the user
sees the rule-bypass warning as its own thing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


def build_telegram_deliver(
    *,
    bot: Any,
    chat_id: int,
    thread_id: int | None,
    main_loop: asyncio.AbstractEventLoop,
) -> Callable[[str, str | None], Awaitable[None]]:
    base_kwargs: dict[str, Any] = {"chat_id": chat_id}
    if thread_id is not None:
        base_kwargs["message_thread_id"] = thread_id

    async def deliver(text: str, system_note: str | None = None) -> None:
        logger.info(
            "mop.deliver entry chat=%s thread=%s text_len=%d note=%s",
            chat_id, thread_id, len(text or ""), bool(system_note),
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(
                bot.send_message(text=text, **base_kwargs), main_loop
            )
            await asyncio.wrap_future(fut)
            if system_note:
                fut2 = asyncio.run_coroutine_threadsafe(
                    bot.send_message(text=f"⚠️ {system_note}", **base_kwargs),
                    main_loop,
                )
                await asyncio.wrap_future(fut2)
            deliver.delivery_count += 1  # type: ignore[attr-defined]
            logger.info("mop.deliver ok chat=%s text_len=%d", chat_id, len(text or ""))
        except Exception:
            logger.exception("mop.deliver FAILED chat=%s text_len=%d", chat_id, len(text or ""))
            raise

    deliver.delivery_count = 0  # type: ignore[attr-defined]
    return deliver
