from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from fastapi import HTTPException

from chats import Chat, save_chats
from config import EXTENSION_PATH, PI_BIN, PI_MODEL, PI_PROVIDER, SYSTEM_PROMPT


def _parse_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                events.append(obj)
        except json.JSONDecodeError:
            pass
    return events


def _find_session_id(events: list[dict]) -> str | None:
    for ev in events:
        if ev.get("type") == "session":
            sid = ev.get("id")
            if isinstance(sid, str):
                return sid
    return None


def _extract_text(events: list[dict]) -> str:
    # agent_end carries the fully assembled messages — simpler and more reliable
    # than assembling streaming text_delta events.
    for ev in reversed(events):
        if ev.get("type") != "agent_end":
            continue
        for msg in reversed(ev.get("messages") or []):
            if msg.get("role") != "assistant":
                continue
            parts = [
                block["text"]
                for block in (msg.get("content") or [])
                if block.get("type") == "text" and block.get("text")
            ]
            if parts:
                return "\n".join(parts).strip()
    return ""


def _find_error(events: list[dict]) -> str | None:
    for ev in events:
        if ev.get("type") in ("message_start", "message_end"):
            msg = ev.get("message") or {}
            if msg.get("stopReason") == "error":
                return msg.get("errorMessage") or "unknown pi error"
    return None


async def run_pi(user_text: str, chat: Chat, model: str | None = None) -> str:
    cmd = [PI_BIN, "-p", "--mode", "json", "--provider", PI_PROVIDER, "--model", model or PI_MODEL]
    if chat.pi_session_id:
        cmd.extend(["--session", chat.pi_session_id])
    cmd.extend(["--append-system-prompt", SYSTEM_PROMPT])
    cmd.extend(["--no-builtin-tools", "--extension", str(EXTENSION_PATH)])
    cmd.append(user_text)

    def _run() -> tuple[str, str, int]:
        r = subprocess.run(cmd, cwd=chat.project_dir, capture_output=True, text=True, timeout=180)
        return r.stdout, r.stderr, r.returncode

    stdout, stderr, rc = await asyncio.to_thread(_run)
    events = _parse_events(stdout)

    new_sid = _find_session_id(events)
    if new_sid and new_sid != chat.pi_session_id:
        chat.pi_session_id = new_sid
        save_chats()

    err = _find_error(events)
    if err:
        print(f"[pi] error: {err}", file=sys.stderr)
        raise HTTPException(status_code=502, detail=f"pi error: {err}")

    text = _extract_text(events)
    if not text and rc != 0:
        raise HTTPException(status_code=502, detail=f"pi exited {rc}: {stderr[:300]}")

    return text or "(no response)"
