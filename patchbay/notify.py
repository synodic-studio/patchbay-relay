"""Standalone, Patchbay-owned Telegram topic notification CLI."""

import argparse
import sys
from dataclasses import dataclass
from typing import TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .outbound import log_outbound
from .standalone import load_bot_token


@dataclass(frozen=True)
class NotificationRequest:
    chat_id: str
    thread_id: str
    text: str
    source: str


@dataclass(frozen=True)
class DeliveryResult:
    succeeded: bool
    code: str


def validate_notification(chat_id: str, thread_id: str, text: str, source: str) -> NotificationRequest:
    if not chat_id or not thread_id.isdigit() or not source or not text or len(text) > 4096:
        raise ValueError("notification fields are invalid or exceed 4096 characters")
    return NotificationRequest(chat_id, thread_id, text, source)


def send_telegram(request: NotificationRequest, token: str) -> bool:
    body = urlencode(
        {
            "chat_id": request.chat_id,
            "message_thread_id": request.thread_id,
            "text": request.text,
        }
    ).encode()
    outbound = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urlopen(outbound, timeout=20) as response:
        return response.status == 200


def deliver(request: NotificationRequest, token: str) -> DeliveryResult:
    try:
        succeeded = send_telegram(request, token)
    except HTTPError:
        return DeliveryResult(succeeded=False, code="TELEGRAM_HTTP_ERROR")
    except (URLError, TimeoutError, OSError):
        return DeliveryResult(succeeded=False, code="TELEGRAM_NETWORK_ERROR")
    except Exception:
        return DeliveryResult(succeeded=False, code="TELEGRAM_SEND_ERROR")
    if not succeeded:
        return DeliveryResult(succeeded=False, code="TELEGRAM_REJECTED")
    log_outbound(f"{request.chat_id}_{request.thread_id}", request.text, request.source)
    return DeliveryResult(succeeded=True, code="DELIVERED")


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a Telegram topic notification through Patchbay")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--stdin", action="store_true", required=True)
    args = parser.parse_args(argv)

    try:
        request = validate_notification(args.chat_id, args.thread_id, (stdin or sys.stdin).read(), args.source)
    except ValueError:
        print("INVALID_NOTIFICATION", file=sys.stderr)
        return 1

    try:
        token = load_bot_token()
    except Exception:
        print("TOKEN_LOAD_ERROR", file=sys.stderr)
        return 1
    if not token:
        print("MISSING_BOT_TOKEN", file=sys.stderr)
        return 1
    result = deliver(request, token)
    if not result.succeeded:
        print(result.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
