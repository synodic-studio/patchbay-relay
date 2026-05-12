"""Outbound file attachments for patchbay.

Sessions (any harness) can ask the bridge to send a local file to the
chat by emitting a sentinel in their response text:

    [[send-file: /absolute/path/to/file.ext]]
    [[send-file: /absolute/path/to/image.png | optional caption]]

The bridge extracts these sentinels before sending the response, strips
them from the user-visible text, and uploads each file via Telegram's
sendPhoto (image/* MIME) or sendDocument (everything else).

Why a text sentinel: it works for every harness (cc-sdk, cc-sdk-mop, pi)
because every harness can produce text. No tool-call plumbing required.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from patchbay.activity import log_activity

logger = logging.getLogger(__name__)

# Telegram hard limits per-file. Anything larger gets rejected before
# the upload attempt so we don't waste bandwidth and can log a clean
# reason.
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # 10MB
DOCUMENT_MAX_BYTES = 50 * 1024 * 1024  # 50MB

_SENTINEL_RE = re.compile(
    r"\[\[send-file:\s*(?P<path>[^|\]]+?)(?:\s*\|\s*(?P<caption>[^\]]+))?\s*\]\]"
)

# Matches inline code spans (backtick-delimited) and fenced code blocks.
# Sentinels inside these are examples/documentation, not real file requests.
_CODE_RE = re.compile(r"```[\s\S]*?```|`[^`]+`")


@dataclass(frozen=True)
class FileRequest:
    """One requested outbound file extracted from response text."""

    path: Path
    caption: str | None


def extract_file_sentinels(text: str) -> tuple[str, list[FileRequest]]:
    """Pull `[[send-file: …]]` sentinels out of text.

    Returns (cleaned_text, requests). The cleaned text has every
    sentinel removed. Sentinels inside backtick code spans or fenced
    blocks are ignored — they're examples, not real file requests.
    """
    requests: list[FileRequest] = []

    # Hide code spans so sentinels inside them are never matched.
    _slots: list[str] = []

    def _hide(m: re.Match[str]) -> str:
        slot = f"\x00SLOT{len(_slots)}\x00"
        _slots.append(m.group())
        return slot

    protected = _CODE_RE.sub(_hide, text)

    def _grab(match: re.Match[str]) -> str:
        raw_path = match.group("path").strip()
        caption_raw = match.group("caption")
        caption = caption_raw.strip() if caption_raw else None
        requests.append(FileRequest(path=Path(raw_path), caption=caption))
        return ""

    cleaned = _SENTINEL_RE.sub(_grab, protected)

    # Restore code spans.
    for i, original in enumerate(_slots):
        cleaned = cleaned.replace(f"\x00SLOT{i}\x00", original)

    # Collapse runs of blank lines created by stripping line-only sentinels.
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned).strip()
    return cleaned, requests


def _classify(path: Path) -> tuple[str, int]:
    """Return (kind, size) where kind is 'photo' or 'document'."""
    mime, _ = mimetypes.guess_type(path.name)
    kind = "photo" if (mime or "").startswith("image/") else "document"
    size = path.stat().st_size
    return kind, size


async def send_files(
    bot,
    *,
    chat_id: int,
    thread_id: int | None,
    session_key: str,
    requests: list[FileRequest],
) -> None:
    """Upload each requested file to the chat.

    Failures are logged to `activity.jsonl` and surfaced to the user as
    a short text note in the same chat. They never raise — a bad file
    path must not break the rest of the response delivery.
    """
    for req in requests:
        path = req.path
        caption = req.caption
        if not path.is_absolute():
            await _report_failure(
                bot,
                chat_id=chat_id,
                thread_id=thread_id,
                session_key=session_key,
                path=path,
                reason="path_not_absolute",
            )
            continue
        if not path.exists() or not path.is_file():
            await _report_failure(
                bot,
                chat_id=chat_id,
                thread_id=thread_id,
                session_key=session_key,
                path=path,
                reason="missing",
            )
            continue
        try:
            kind, size = _classify(path)
        except OSError as exc:
            await _report_failure(
                bot,
                chat_id=chat_id,
                thread_id=thread_id,
                session_key=session_key,
                path=path,
                reason=f"stat_failed:{type(exc).__name__}",
            )
            continue

        cap = DOCUMENT_MAX_BYTES if kind == "document" else PHOTO_MAX_BYTES
        if size > cap:
            await _report_failure(
                bot,
                chat_id=chat_id,
                thread_id=thread_id,
                session_key=session_key,
                path=path,
                reason=f"too_large:{size}>{cap}",
            )
            continue

        send_kwargs: dict = {"chat_id": chat_id}
        if thread_id is not None:
            send_kwargs["message_thread_id"] = thread_id
        if caption:
            send_kwargs["caption"] = caption

        try:
            with path.open("rb") as fh:
                if kind == "photo":
                    await bot.send_photo(photo=fh, **send_kwargs)
                else:
                    await bot.send_document(document=fh, **send_kwargs)
        except Exception as exc:  # noqa: BLE001 — log and keep going
            logger.warning(
                "outbound file send failed (%s) chat=%s thread=%s path=%s: %s",
                type(exc).__name__,
                chat_id,
                thread_id,
                path,
                exc,
            )
            await _report_failure(
                bot,
                chat_id=chat_id,
                thread_id=thread_id,
                session_key=session_key,
                path=path,
                reason=f"send_failed:{type(exc).__name__}:{str(exc)[:120]}",
            )
            continue

        log_activity(
            "outbound_file_sent",
            session_key=session_key,
            path=str(path),
            kind=kind,
            size=size,
            caption_len=len(caption) if caption else 0,
        )


async def _report_failure(
    bot,
    *,
    chat_id: int,
    thread_id: int | None,
    session_key: str,
    path: Path,
    reason: str,
) -> None:
    """Log + notify the chat that a file couldn't be sent."""
    log_activity(
        "outbound_file_failed",
        session_key=session_key,
        path=str(path),
        reason=reason,
    )
    send_kwargs: dict = {"chat_id": chat_id}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id
    try:
        await bot.send_message(
            text=f"[file send failed: {path.name} — {reason}]",
            **send_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("file-send failure notice itself failed: %s", exc)
