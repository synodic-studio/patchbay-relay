"""Telegram delivery closure for MOP.

build_telegram_deliver(bot, chat_id, thread_id, main_loop, session_key)
returns a Deliver-shaped async callable: `(text, system_note) -> None`.
The closure schedules every outbound Telegram call on the bridge's main
asyncio loop via `run_coroutine_threadsafe`, because the cc-sdk-mop
dispatch invokes this deliver from inside `asyncio.run(_drive_mop_session())`
running in a thread-pool worker. Calling `bot.send_message` directly on
the temporary loop binds httpx connections to a loop that closes when
the SDK turn ends — that poisons the bot for any subsequent main-loop
request (handle_photo, the next /command, etc.) with `RuntimeError:
Event loop is closed`. Routing through `main_loop` keeps every Telegram
I/O on the loop where the bot's httpx client was created.

When system_note is set, we send TWO Telegram messages: the user
message unchanged (minus any file sentinels), then a separate bubble
with a clear marker prefix so the user sees the rule-bypass warning as
its own thing.

File-attachment sentinels (`[[send-file: /abs/path | caption]]`) inside
the delivered text are extracted the same way the cc-sdk / pi path
does it in `telegram_send.py`: parse out the sentinels, strip them
from the user-visible text, and route each request through
`file_send.send_files` so the file ships as a real Telegram attachment
instead of leaking through as plain text.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from patchbay.file_send import extract_file_sentinels, send_files

logger = logging.getLogger(__name__)


def build_telegram_deliver(
    *,
    bot: Any,
    chat_id: int,
    thread_id: int | None,
    main_loop: asyncio.AbstractEventLoop,
    session_key: str | None = None,
) -> Callable[[str, str | None], Awaitable[None]]:
    base_kwargs: dict[str, Any] = {"chat_id": chat_id}
    if thread_id is not None:
        base_kwargs["message_thread_id"] = thread_id

    # Fall back to a synthetic session key if the caller didn't provide one.
    # `send_files` only uses this for activity-log entries and the in-chat
    # failure note; a synthetic value is harmless but the caller should
    # always pass the real key when available.
    resolved_session_key = session_key or f"{chat_id}_{thread_id or 0}"

    async def deliver(text: str, system_note: str | None = None) -> None:
        cleaned, file_requests = extract_file_sentinels(text or "")
        logger.info(
            "mop.deliver entry chat=%s thread=%s text_len=%d files=%d note=%s",
            chat_id,
            thread_id,
            len(cleaned),
            len(file_requests),
            bool(system_note),
        )
        try:
            if cleaned:
                fut = asyncio.run_coroutine_threadsafe(
                    bot.send_message(text=cleaned, **base_kwargs), main_loop
                )
                await asyncio.wrap_future(fut)
            if file_requests:
                # `send_files` issues its own bot.* calls; route them through
                # main_loop for the same connection-pool-isolation reason the
                # text send uses.
                fut_files = asyncio.run_coroutine_threadsafe(
                    send_files(
                        bot,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        session_key=resolved_session_key,
                        requests=file_requests,
                    ),
                    main_loop,
                )
                await asyncio.wrap_future(fut_files)
            if system_note:
                fut2 = asyncio.run_coroutine_threadsafe(
                    bot.send_message(text=f"⚠️ {system_note}", **base_kwargs),
                    main_loop,
                )
                await asyncio.wrap_future(fut2)
            deliver.delivery_count += 1  # type: ignore[attr-defined]
            logger.info(
                "mop.deliver ok chat=%s text_len=%d files=%d",
                chat_id,
                len(cleaned),
                len(file_requests),
            )
        except Exception:
            logger.exception(
                "mop.deliver FAILED chat=%s text_len=%d files=%d",
                chat_id,
                len(cleaned),
                len(file_requests),
            )
            raise

    deliver.delivery_count = 0  # type: ignore[attr-defined]
    return deliver
