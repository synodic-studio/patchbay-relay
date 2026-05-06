#!/usr/bin/env python3
"""TCC prompt watcher — fires Telegram alert the instant macOS shows a TCC dialog.

Runs as a persistent launchd daemon. Streams system log from tccd and sends
an immediate Telegram message when a consent dialog appears, so you know
within seconds instead of finding out an hour later.
"""

import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CHAT_ID = -1003884282041
THREAD_ID = 30
DEDUP_WINDOW_S = 120

# Phrases that appear when tccd actually shows a dialog to the user.
# Deliberately narrow — silent denials/approvals must not match.
PROMPT_SIGNALS = [
    "Posting blocking TCC dialog",
    "posting blocking TCC dialog",
    "Prompting user",
    "prompting user",
    "Present dialog",
    "present dialog",
    "tcc_prompt",
]

# Lines containing any of these are silent denials — no dialog was shown.
DENY_SIGNALS = [
    "does not allow prompting",
    "returning denied",
    "not entitled",
    "Access denied",
    "access denied",
]

KNOWN_BINARIES = ["mise", "uv", "python", "python3", "brew", "ruby", "node"]


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) > 0:
        return ctx
    # Python 3.14 framework has an empty cert store in launchd envs.
    # Try certifi bundle first, then /etc/ssl/cert.pem.
    for cafile in (
        "/Library/Frameworks/Python.framework/Versions/3.14/lib/python3.14/site-packages/certifi/cacert.pem",
        "/etc/ssl/cert.pem",
    ):
        if Path(cafile).is_file():
            ctx.load_verify_locations(cafile=cafile)
            if ctx.cert_store_stats().get("x509_ca", 0) > 0:
                return ctx
    return ctx


def _bot_token() -> str:
    try:
        r = subprocess.run(
            ["pass", "show", "telegram-bot-token"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip().split("\n")[0]
    except Exception:
        pass
    import os
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _send(text: str, token: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "message_thread_id": THREAD_ID,
        "text": text,
    }).encode()
    try:
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx()):
            pass
    except Exception as e:
        print(f"[tcc-watcher] send failed: {e}", file=sys.stderr, flush=True)


def _binary_name(line: str) -> str:
    m = re.search(
        r"(/[^\s,;\"']+(?:mise|uv|python|brew|ruby|node)[^\s,;\"']*)",
        line, re.IGNORECASE,
    )
    if m:
        return Path(m.group(1)).name
    for name in KNOWN_BINARIES:
        if name in line.lower():
            return name
    m2 = re.search(r"process[:\s]+([A-Za-z0-9_.-]+)", line)
    if m2:
        return m2.group(1)
    return "unknown"


def _is_prompt_line(line: str) -> bool:
    if "kTCCService" not in line:
        return False
    if any(sig in line for sig in DENY_SIGNALS):
        return False
    return any(sig in line for sig in PROMPT_SIGNALS)


def watch() -> None:
    token = _bot_token()
    if not token:
        print("[tcc-watcher] ERROR: no bot token — exiting", file=sys.stderr, flush=True)
        sys.exit(1)

    print("[tcc-watcher] started", flush=True)
    last_alerted: dict[str, float] = {}

    while True:
        try:
            proc = subprocess.Popen(
                ["log", "stream", "--predicate", 'process == "tccd"', "--style", "compact"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            for raw in proc.stdout:
                line = raw.strip()
                if not _is_prompt_line(line):
                    continue
                binary = _binary_name(line)
                now = time.monotonic()
                if now - last_alerted.get(binary, 0) < DEDUP_WINDOW_S:
                    continue
                last_alerted[binary] = now
                msg = (
                    f"TCC dialog waiting: {binary}\n\n"
                    f"Approve in System Settings > Privacy & Security on the Mac Mini.\n\n"
                    f"Log: {line[:300]}"
                )
                print(f"[tcc-watcher] alert: {binary}", flush=True)
                _send(msg, token)
            proc.wait()
        except Exception as e:
            print(f"[tcc-watcher] error: {e} — restarting in 5s", file=sys.stderr, flush=True)
        time.sleep(5)


if __name__ == "__main__":
    watch()
