"""Telegram outbound send helpers.

Extracted from `bridge.py`. Contains the four functions that get bytes
to Telegram:

  * ``keep_typing`` — typing-indicator background loop with bounded retries
  * ``_to_markdownv2`` — wrap telegramify_markdown.markdownify with logging
  * ``_send_response`` — split + send a response, downgrade to plain on
    unbalanced MarkdownV2 toggles
  * ``_notify_delivery_failure`` — best-effort error message to the user

Test-patch compatibility: tests patch ``bridge.X`` (constants, helper
funcs, telegramify_markdown). To keep those patches effective, this
module reaches back through ``bridge`` for the values it consumes at
call time — same pattern as ``patchbay/commands/*``. The module
imports ``bridge`` at top; this is safe because ``bridge`` re-exports
these symbols at the very end of its body, so the cycle is resolved
by then.

Likewise, the caplog tests filter on ``logger="bridge"``, so all
log records emitted from here go through ``bridge.logger`` rather
than a per-module logger.
"""

from __future__ import annotations

import asyncio
import re

from telegram.constants import ParseMode
from telegram.error import ChatMigrated, Forbidden, RetryAfter

import bridge


TYPING_MAX_FAILURES = 5

# ---------------------------------------------------------------------------
# Response filters (Feature 3 + Feature 6)
# ---------------------------------------------------------------------------

_SILENCE_NARRATION_RE = re.compile(
    r"^[\s*_~`]*\(?\s*(?:silent|silence|no\s+response|no\s+reply)\s*\.?\)?[\s*_~`]*$"
    r"|^[\s*_~`]*[\U0001F507\.…]+[\s*_~`]*$",
    re.IGNORECASE,
)

_NOISY_STATUS_RE = re.compile(
    r"^compacting\s+context(?:\s*[—\-]\s*summariz\w*(?:\s+\w+)*)?[\s.…]*$"
    r"|^rate\s+limited[,.]?\s+waiting\s+\d+\s*s?[\s.…]*$"
    r"|^retrying\s+in\s+\d+\s*s[\s.…]*$",
    re.IGNORECASE,
)


def _is_silence_narration(text: str) -> bool:
    """Return True if `text` is purely a silence-narration token (e.g. *(silent)*, 🔇)."""
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return False
    return bool(_SILENCE_NARRATION_RE.match(stripped))


def _is_noisy_status(text: str) -> bool:
    """Return True if `text` is pure internal-status chatter that shouldn't reach the user."""
    stripped = text.strip()
    if not stripped or len(stripped) > 200:
        return False
    return bool(_NOISY_STATUS_RE.match(stripped))


async def keep_typing(
    chat_id: int,
    thread_id: int | None,
    stop_event: asyncio.Event,
    bot,
) -> None:
    """Send typing indicator every few seconds until stop_event is set.

    Error policy (nothing is silently swallowed):

      * ``Forbidden`` / ``ChatMigrated`` — persistent; log once and give up
        immediately. The bot has been blocked, kicked, or the chat moved.
      * ``RetryAfter`` — honor the server-requested backoff instead of the
        default interval.
      * Other exceptions — log at warning, keep trying; give up after
        ``TYPING_MAX_FAILURES`` consecutive failures. Success resets the
        counter so a periodic transient blip never trips the cap.
      * ``CancelledError`` — propagate so awaiters see the cancellation.
    """
    kwargs = {"chat_id": chat_id, "action": "typing"}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id

    failures = 0
    wait_seconds: float = bridge.TYPING_INTERVAL
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(**kwargs)
            failures = 0
            wait_seconds = bridge.TYPING_INTERVAL
        except asyncio.CancelledError:
            raise
        except (Forbidden, ChatMigrated) as exc:
            bridge.logger.warning(
                "keep_typing giving up for chat=%s thread=%s: %s (persistent)",
                chat_id,
                thread_id,
                type(exc).__name__,
            )
            return
        except RetryAfter as exc:
            wait_seconds = float(getattr(exc, "retry_after", bridge.TYPING_INTERVAL))
            bridge.logger.warning(
                "keep_typing rate-limited for chat=%s thread=%s; backing off %.1fs",
                chat_id,
                thread_id,
                wait_seconds,
            )
            # do not count RetryAfter toward the give-up cap — Telegram told
            # us to wait, not that we've failed.
        except Exception as exc:
            failures += 1
            max_failures = bridge.TYPING_MAX_FAILURES
            log_fn = bridge.logger.error if failures >= max_failures else bridge.logger.warning
            log_fn(
                "keep_typing failed for chat=%s thread=%s (%d/%d): %s: %s",
                chat_id,
                thread_id,
                failures,
                max_failures,
                type(exc).__name__,
                exc,
            )
            if failures >= max_failures:
                bridge.logger.error(
                    "keep_typing giving up for chat=%s thread=%s after %d consecutive failures",
                    chat_id,
                    thread_id,
                    failures,
                )
                return

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
            return
        except asyncio.TimeoutError:
            continue


async def _run_heartbeat(
    chat_id: int,
    thread_id: int | None,
    stop_event: asyncio.Event,
    bot,
    start_time: float,
    msg_holder: list,
    *,
    delay: float,
    interval: float,
) -> None:
    """Send an edit-in-place ⏳ Working — N min bubble after a delay.

    Waits `delay` seconds; if the turn finishes before then (stop_event set),
    exits silently. After the delay, sends the bubble and appends its
    message_id to `msg_holder` so the caller can delete it on success.
    Edits the bubble every `interval` seconds thereafter. All Telegram errors
    are swallowed — the heartbeat is best-effort and must never affect delivery.
    """
    import time as _time

    send_kwargs: dict = {"chat_id": chat_id}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
        return  # turn finished before first bubble
    except asyncio.TimeoutError:
        pass

    elapsed_min = int(((_time.time() - start_time) / 60) + 0.5)
    try:
        msg = await bot.send_message(text=f"⏳ Working — {elapsed_min} min", **send_kwargs)
        msg_holder.append(msg.message_id)
        msg_id = msg.message_id
    except Exception as exc:
        bridge.logger.debug("heartbeat send failed for chat=%s: %s", chat_id, exc)
        return

    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return  # turn finished during edit loop
        except asyncio.TimeoutError:
            pass

        elapsed_min = int(((_time.time() - start_time) / 60) + 0.5)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=f"⏳ Working — {elapsed_min} min",
            )
        except Exception as exc:
            bridge.logger.debug("heartbeat edit failed for chat=%s: %s", chat_id, exc)
            return


_MARKDOWN_FAILURE_TEXT_LIMIT = 800


def _to_markdownv2(text: str) -> str | None:
    """Convert markdown to Telegram MarkdownV2. Returns None on failure.

    On conversion failure, log the raw text (truncated) and the exception
    to activity.jsonl as event=markdown_conversion_failed so we can come
    back later and reproduce the bug in the converter. Without this, a
    quiet plain-text fallback hides converter regressions.
    """
    try:
        return bridge.telegramify_markdown.markdownify(text)
    except Exception as exc:
        bridge._log_activity(
            "markdown_conversion_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
            raw_text=text[:_MARKDOWN_FAILURE_TEXT_LIMIT],
            raw_text_len=len(text),
            truncated=len(text) > _MARKDOWN_FAILURE_TEXT_LIMIT,
        )
        bridge.logger.warning(
            "MarkdownV2 conversion failed (%s): %s — see activity.jsonl for raw text",
            type(exc).__name__,
            str(exc)[:120],
        )
        return None


async def _send_response(bot, chat_id: int, thread_id: int | None, response: str) -> None:
    """Send a response, splitting at Telegram's message limit.

    The response is converted to MarkdownV2 *once as a whole* (not per
    chunk) and then split on paragraph > line > word boundaries. Splitting
    converted text — rather than slicing the raw response and converting
    each slice independently — prevents mid-`*bold*` cuts that yielded
    Telegram's `BadRequest: can't find end of bold entity` errors. After
    splitting, each chunk's toggle entities (`*`, `_`, ``` `, ``` ``` ```,
    `~`, `||`) are checked for parity; if any chunk is unbalanced, the
    entire response downgrades to plain text (the same all-or-nothing
    invariant the previous chunker enforced — no half-formatted output).

    If a chunk's MarkdownV2 send fails mid-response, remaining chunks
    downgrade to plain too. Retries each chunk up to SEND_RETRY_ATTEMPTS
    times with exponential backoff.

    Every send attempt's outcome is recorded to the outbound audit log
    (source="claude-response") for diagnosing client-side render drops.
    Audit failures are swallowed: they must never affect user-visible
    send behavior.
    """
    send_kwargs: dict = {"chat_id": chat_id}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id
    audit_session_key = bridge._session_key(chat_id, thread_id)

    response, file_requests = bridge.extract_file_sentinels(response)

    if not response and not file_requests:
        return

    # Feature 6: drop pure-silence narration (*(silent)*, 🔇, etc.)
    if response and _is_silence_narration(response):
        bridge._log_activity(
            "silence_narration_filtered",
            session_key=audit_session_key,
            text=response[:80],
        )
        bridge.logger.info("Filtered silence narration for %s: %r", audit_session_key, response[:40])
        response = ""
        if not file_requests:
            return

    # Feature 3: drop pure internal-status chatter (compaction narration, etc.)
    if response and _is_noisy_status(response):
        bridge._log_activity(
            "noisy_status_filtered",
            session_key=audit_session_key,
            text=response[:80],
        )
        bridge.logger.info("Filtered noisy status for %s: %r", audit_session_key, response[:40])
        response = ""
        if not file_requests:
            return

    # Convert the whole response once, then split the converted MarkdownV2
    # text on paragraph/line/word boundaries so chunks never cut mid-entity.
    # The parity check is defense-in-depth: the 2026-05-11 heading-prefix
    # fix removed the only known converter bug that produced unbalanced
    # output, but if a future converter regression slips one through we'd
    # rather downgrade the whole response to plain than ship a chunk
    # Telegram will reject with `can't find end of bold entity`.
    converted_full = bridge._to_markdownv2(response) if response else None
    raw_chunks: list[str]
    md_chunks: list[str | None]
    if converted_full is None:
        raw_chunks = bridge.split_for_telegram(response, bridge.TELEGRAM_MSG_LIMIT) if response else []
        md_chunks = [None] * len(raw_chunks)
        use_markdown = False
    else:
        md_pieces = bridge.split_for_telegram(converted_full, bridge.TELEGRAM_MSG_LIMIT)
        if all(bridge.is_markdownv2_balanced(m) for m in md_pieces):
            # Audit `raw` field is best-effort: for single-chunk responses
            # it's the full original markdown (perfect fidelity); for
            # multi-chunk we fall back to a parallel split of the source so
            # diagnostics still see source-shaped slices even if the byte
            # offsets don't line up exactly with the md chunks.
            if len(md_pieces) == 1:
                raw_chunks = [response]
            else:
                raw_chunks = bridge.split_for_telegram(response, bridge.TELEGRAM_MSG_LIMIT)
                if len(raw_chunks) != len(md_pieces):
                    # Pad/truncate raw to match md count so the audit log
                    # has one raw per md chunk.
                    raw_chunks = (raw_chunks + [""] * len(md_pieces))[: len(md_pieces)]
            md_chunks = list(md_pieces)
            use_markdown = True
        else:
            bridge._log_activity(
                "markdown_chunk_unbalanced",
                session_key=audit_session_key,
                chunk_total=len(md_pieces),
                converted_len=len(converted_full),
                response_len=len(response),
            )
            bridge.logger.warning(
                "MarkdownV2 chunk parity check failed (%d chunks); "
                "downgrading entire response to plain to avoid Telegram "
                "entity rejection",
                len(md_pieces),
            )
            raw_chunks = bridge.split_for_telegram(response, bridge.TELEGRAM_MSG_LIMIT)
            md_chunks = [None] * len(raw_chunks)
            use_markdown = False

    chunk_total = len(raw_chunks)
    if not chunk_total and not file_requests:
        return

    for chunk_index, chunk in enumerate(raw_chunks):
        md_chunk = md_chunks[chunk_index] if use_markdown else None
        last_exc: Exception | None = None
        for attempt in range(bridge.SEND_RETRY_ATTEMPTS):
            sent_as_md = md_chunk is not None
            try:
                if sent_as_md:
                    sent_msg = await bot.send_message(
                        text=md_chunk,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        **send_kwargs,
                    )
                else:
                    sent_msg = await bot.send_message(text=chunk, **send_kwargs)
                # Feature 5: record sent text for reply-context injection
                try:
                    from patchbay.reply_store import record as _rs_record

                    _rs_record(chat_id, sent_msg.message_id, chunk)
                except Exception:
                    pass
                last_exc = None
                try:
                    bridge.log_outbound_response(
                        session_key=audit_session_key,
                        chunk_index=chunk_index,
                        chunk_total=chunk_total,
                        raw=chunk,
                        md=md_chunk if sent_as_md else None,
                        parse_mode="MarkdownV2" if sent_as_md else "plain",
                        status="ok",
                    )
                except Exception as audit_exc:
                    bridge.logger.debug("outbound audit log failed (success path): %s", audit_exc)
                break
            except Exception as e:
                last_exc = e
                # MarkdownV2 send failed: drop to plain for this attempt and
                # propagate the downgrade to all remaining chunks of this
                # response so we don't ship a half-formatted message.
                if md_chunk is not None:
                    # Log raw + md so we can reproduce the converter bug or
                    # the Telegram-rejected payload later.
                    bridge._log_activity(
                        "markdown_send_failed",
                        session_key=audit_session_key,
                        error_type=type(e).__name__,
                        error=str(e)[:300],
                        chunk_index=chunk_index,
                        chunk_total=chunk_total,
                        raw_text=chunk[:_MARKDOWN_FAILURE_TEXT_LIMIT],
                        md_text=md_chunk[:_MARKDOWN_FAILURE_TEXT_LIMIT],
                        raw_text_len=len(chunk),
                        md_text_len=len(md_chunk),
                    )
                    bridge.logger.warning(
                        "MarkdownV2 send rejected (%s) chunk %d/%d, "
                        "switching this and all remaining chunks to plain — "
                        "see activity.jsonl markdown_send_failed for raw+md",
                        type(e).__name__,
                        chunk_index + 1,
                        chunk_total,
                    )
                    md_chunk = None
                    use_markdown = False
                delay = bridge.SEND_RETRY_BASE_DELAY * (2**attempt)
                bridge.logger.warning(
                    "Telegram send failed (attempt %d/%d) for chat=%s thread=%s chunk %d/%d: %s — retrying in %.1fs",
                    attempt + 1,
                    bridge.SEND_RETRY_ATTEMPTS,
                    chat_id,
                    thread_id,
                    chunk_index + 1,
                    chunk_total,
                    e,
                    delay,
                )
                if attempt < bridge.SEND_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(delay)
        if last_exc is not None:
            bridge.logger.error(
                "Telegram send failed after %d attempts for chat=%s thread=%s chunk %d/%d: %s",
                bridge.SEND_RETRY_ATTEMPTS,
                chat_id,
                thread_id,
                chunk_index + 1,
                chunk_total,
                last_exc,
            )
            try:
                bridge.log_outbound_response(
                    session_key=audit_session_key,
                    chunk_index=chunk_index,
                    chunk_total=chunk_total,
                    raw=chunk,
                    md=None,
                    parse_mode="plain" if md_chunk is None else "MarkdownV2",
                    status=type(last_exc).__name__,
                )
            except Exception as audit_exc:
                bridge.logger.debug("outbound audit log failed (error path): %s", audit_exc)
            raise last_exc

    if file_requests:
        await bridge.send_files(
            bot,
            chat_id=chat_id,
            thread_id=thread_id,
            session_key=audit_session_key,
            requests=file_requests,
        )


async def _notify_delivery_failure(bot, chat_id: int, thread_id: int | None, label: str) -> None:
    """Attempt to notify the user that a response failed to deliver."""
    send_kwargs: dict = {"chat_id": chat_id}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id
    try:
        await bot.send_message(
            text="[Response was generated but could not be delivered due to a Telegram error. Check logs for details.]",
            **send_kwargs,
        )
    except Exception as notify_exc:
        bridge.logger.error(
            "Also failed to send delivery-failure notification for %s: %s",
            label,
            notify_exc,
        )
