#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
#     "python-multipart",
#     "faster-whisper",
# ]
# ///
"""
Voice-chat demo server — pi backend, multi-chat, Tailscale-hosted.

Pipeline per press-to-talk turn:
    phone records → POST /api/talk
        → faster-whisper (local ASR)
        → pi --provider litellm --model small (local proxy, per-chat session)
        → macOS say → AAC/m4a
    → audio streamed back, plays via AudioContext

Run:
    cd voice-demo && uv run server.py

Then expose via Tailscale (provides HTTPS — required for mic access):
    tailscale serve --bg http://localhost:8800
    # page is now at https://bajor.<tailnet>.ts.net
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST = os.environ.get("VOICE_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOICE_PORT", "8800"))

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")

PI_BIN = os.environ.get("PI_BIN", shutil.which("pi") or "pi")
PI_PROVIDER = os.environ.get("PI_PROVIDER", "litellm")
PI_MODEL = os.environ.get("PI_MODEL", "small")

DEVELOPER_DIR = Path(os.environ.get("DEVELOPER_DIR", os.path.expanduser("~/Developer")))
CHATS_FILE = Path(os.environ.get("CHATS_FILE", os.path.expanduser("~/.voice-demo-chats.json")))

TTS_VOICE = os.environ.get("TTS_VOICE", "Samantha").strip()

SYSTEM_PROMPT = """You are a voice coding assistant accessed from a mobile phone. The user speaks to you and your replies are read aloud by text-to-speech. Follow these rules strictly at all times:

Speak in plain English only. Never use markdown, headings, bullet points, numbered lists, code blocks, backticks, bold, italics, URLs, or any other formatting meant for visual reading. Write exactly as you would speak to someone on a phone call.

Keep answers short and conversational. One to three sentences unless the user clearly needs more. When referring to code, describe it in plain words rather than quoting syntax.

If you need to write anything to disk, you may only create or edit files inside the docs/patchbay/ directory within the current project. Do not modify any source code or any files outside of docs/patchbay/. If asked to edit code directly, explain what change to make instead of doing it."""

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
AUDIO_DIR = Path(tempfile.gettempdir()) / "voice-demo-audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Chat persistence
# ---------------------------------------------------------------------------


@dataclass
class Chat:
    id: str  # stable UUID for this chat (used in API URLs)
    name: str  # display name — basename of project_dir
    project_dir: str  # absolute path under ~/Developer
    # pi session UUID extracted from pi's first event; None until first turn
    pi_session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


_chats: dict[str, Chat] = {}


def _load_chats() -> None:
    if not CHATS_FILE.exists():
        return
    try:
        raw = json.loads(CHATS_FILE.read_text())
        for c in raw.get("chats", []):
            chat = Chat(**{k: c[k] for k in Chat.__dataclass_fields__ if k in c})
            _chats[chat.id] = chat
    except Exception as exc:
        print(f"[chats] load failed: {exc}", file=sys.stderr)


def _save_chats() -> None:
    CHATS_FILE.write_text(json.dumps({"chats": [asdict(c) for c in _chats.values()]}, indent=2))


def _chat_json(c: Chat) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "project_dir": c.project_dir,
        "created_at": c.created_at,
        "last_active": c.last_active,
    }


# ---------------------------------------------------------------------------
# Pi runner
# ---------------------------------------------------------------------------


def _parse_pi_events(stdout: str) -> list[dict]:
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
    texts: list[str] = []
    pending: dict[int, list[str]] = {}
    for ev in events:
        if ev.get("type") != "message_update":
            continue
        ame = ev.get("assistantMessageEvent") or {}
        kind = ame.get("type")
        idx = ame.get("contentIndex", 0)
        if kind == "text_delta":
            delta = ame.get("delta", "")
            if isinstance(delta, str) and delta:
                pending.setdefault(idx, []).append(delta)
        elif kind == "text_end":
            content = ame.get("content")
            if isinstance(content, str) and content:
                texts.append(content)
                pending.pop(idx, None)
            elif idx in pending:
                texts.append("".join(pending.pop(idx)))
    for chunk in pending.values():
        texts.append("".join(chunk))
    return "\n".join(t for t in texts if t).strip()


def _find_error(events: list[dict]) -> str | None:
    for ev in events:
        if ev.get("type") in ("message_start", "message_end"):
            msg = ev.get("message") or {}
            if msg.get("stopReason") == "error":
                return msg.get("errorMessage") or "unknown pi error"
    return None


async def run_pi(user_text: str, chat: Chat) -> str:
    cmd = [PI_BIN, "-p", "--mode", "json", "--provider", PI_PROVIDER, "--model", PI_MODEL]
    if chat.pi_session_id:
        cmd.extend(["--session", chat.pi_session_id])
    cmd.extend(["--append-system-prompt", SYSTEM_PROMPT])
    cmd.extend(["--tools", "read,grep,find,ls,write"])
    cmd.append(user_text)

    def _run() -> tuple[str, str, int]:
        result = subprocess.run(
            cmd,
            cwd=chat.project_dir,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return result.stdout, result.stderr, result.returncode

    stdout, stderr, rc = await asyncio.to_thread(_run)
    events = _parse_pi_events(stdout)

    # Store session ID from first turn so subsequent turns resume it.
    new_sid = _find_session_id(events)
    if new_sid and new_sid != chat.pi_session_id:
        chat.pi_session_id = new_sid
        _save_chats()

    err = _find_error(events)
    if err:
        print(f"[pi] error: {err}", file=sys.stderr)
        raise HTTPException(status_code=502, detail=f"pi error: {err}")

    text = _extract_text(events)
    if not text and rc != 0:
        raise HTTPException(status_code=502, detail=f"pi exited {rc}: {stderr[:300]}")

    return text or "(no response)"


# ---------------------------------------------------------------------------
# Whisper (lazy load on first use)
# ---------------------------------------------------------------------------

_whisper_model = None
_whisper_lock = asyncio.Lock()


async def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        async with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel

                print(f"[whisper] loading model={WHISPER_MODEL} ...", flush=True)
                t0 = time.time()
                _whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
                print(f"[whisper] loaded in {time.time() - t0:.1f}s", flush=True)
    return _whisper_model


async def transcribe(audio_path: Path) -> str:
    model = await get_whisper()

    def _run() -> str:
        segments, _ = model.transcribe(str(audio_path), vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    return await asyncio.to_thread(_run)


async def synthesize(text: str) -> Path:
    if not shutil.which("say"):
        raise HTTPException(status_code=500, detail="`say` not found — macOS only")
    uid = uuid.uuid4().hex
    aiff = AUDIO_DIR / f"{uid}.aiff"
    out = AUDIO_DIR / f"{uid}.m4a"
    say_cmd = ["say", "-v", TTS_VOICE, "-o", str(aiff), text]

    def _run() -> Path:
        subprocess.run(say_cmd, check=True, capture_output=True)
        if shutil.which("afconvert"):
            subprocess.run(
                ["afconvert", str(aiff), str(out), "-f", "m4af", "-d", "aac"],
                check=True,
                capture_output=True,
            )
            aiff.unlink(missing_ok=True)
            return out
        return aiff

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="voice-demo")


@app.on_event("startup")
async def startup():
    _load_chats()
    print(f"[voice-demo] http://{HOST}:{PORT}  pi={PI_PROVIDER}/{PI_MODEL}", flush=True)
    print(f"[voice-demo] {len(_chats)} chat(s) loaded from {CHATS_FILE}", flush=True)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "whisper_model": WHISPER_MODEL,
        "whisper_loaded": _whisper_model is not None,
        "pi_bin": PI_BIN,
        "pi_provider": PI_PROVIDER,
        "pi_model": PI_MODEL,
        "tts_voice": TTS_VOICE,
        "chat_count": len(_chats),
        "developer_dir": str(DEVELOPER_DIR),
    }


# ---- Chat management ----


@app.get("/api/chats")
async def list_chats():
    ordered = sorted(_chats.values(), key=lambda c: -c.last_active)
    return {"chats": [_chat_json(c) for c in ordered]}


@app.post("/api/chats")
async def create_chat(body: dict):
    rel = body.get("project_dir", "").strip()
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise HTTPException(status_code=400, detail="project_dir must be a name under DEVELOPER_DIR")
    base = DEVELOPER_DIR.resolve()
    path = (base / rel).resolve()
    if not path.is_dir() or (base not in path.parents and path != base):
        raise HTTPException(status_code=400, detail="project_dir must resolve under DEVELOPER_DIR")
    chat = Chat(id=uuid.uuid4().hex, name=path.name, project_dir=str(path))
    _chats[chat.id] = chat
    _save_chats()
    return _chat_json(chat)


@app.delete("/api/chats/{chat_id}")
async def close_chat(chat_id: str):
    if chat_id not in _chats:
        raise HTTPException(status_code=404, detail="Chat not found")
    del _chats[chat_id]
    _save_chats()
    return {"ok": True}


@app.post("/api/chats/{chat_id}/reset")
async def reset_chat(chat_id: str):
    if chat_id not in _chats:
        raise HTTPException(status_code=404, detail="Chat not found")
    _chats[chat_id].pi_session_id = None  # cleared — next turn starts fresh
    _save_chats()
    return _chat_json(_chats[chat_id])


# ---- Project listing ----


@app.get("/api/projects")
async def list_projects():
    try:
        dirs = sorted(d.name for d in DEVELOPER_DIR.iterdir() if d.is_dir() and not d.name.startswith("."))
        return {"projects": dirs, "base": str(DEVELOPER_DIR)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Talk ----


@app.post("/api/talk")
async def talk(audio: UploadFile = File(...), chat_id: str = Form(...)):
    if chat_id not in _chats:
        raise HTTPException(status_code=404, detail="Chat not found")
    chat = _chats[chat_id]

    t0 = time.time()
    suffix = Path(audio.filename or "clip.webm").suffix or ".webm"
    tmp = AUDIO_DIR / f"in-{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(await audio.read())

    try:
        transcript = await transcribe(tmp)
        t_asr = time.time()

        if not transcript:
            return JSONResponse({"transcript": "", "reply": "", "audio_url": None, "note": "No speech detected."})

        reply = await run_pi(transcript, chat)
        t_llm = time.time()

        audio_path = await synthesize(reply)
        t_tts = time.time()

        chat.last_active = time.time()
        _save_chats()

        print(
            f"[turn] chat={chat.name} "
            f"asr={t_asr - t0:.1f}s llm={t_llm - t_asr:.1f}s tts={t_tts - t_llm:.1f}s "
            f"total={t_tts - t0:.1f}s | q={transcript!r} a={reply[:80]!r}",
            flush=True,
        )

        return JSONResponse(
            {
                "transcript": transcript,
                "reply": reply,
                "audio_url": f"/audio/{audio_path.name}",
                "timing": {
                    "asr": round(t_asr - t0, 2),
                    "llm": round(t_llm - t_asr, 2),
                    "tts": round(t_tts - t_llm, 2),
                    "total": round(t_tts - t0, 2),
                },
            }
        )
    finally:
        tmp.unlink(missing_ok=True)


# ---- Audio serving ----


@app.get("/audio/{name}")
async def get_audio(name: str):
    safe = Path(name).name
    path = AUDIO_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="audio expired")
    media = "audio/mp4" if path.suffix == ".m4a" else "audio/aiff"
    return FileResponse(path, media_type=media)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
