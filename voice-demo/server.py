#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
#     "python-multipart",
#     "faster-whisper",
#     "httpx",
# ]
# ///
"""
Voice-chat demo server — runs on the Mac, exposed to the phone via cloudflared.

Pipeline for one press-to-talk turn:

    phone records audio (hold button)
        -> POST /api/talk  (multipart audio blob)
        -> faster-whisper transcribes locally
        -> litellm "small" alias (OpenAI-compatible proxy) generates a reply
        -> macOS `say` synthesizes the reply to audio (afconvert -> m4a/aac)
        -> JSON {transcript, reply, audio_url} back to the phone
        -> phone plays /audio/<id> in an <audio> element

This is a *prototype* to prove the loop. It is deliberately single-process and
stateless-per-turn. The eventual goal (per the patchbay-relay vision) is to let
this "speak with a repo" and one day drive a narrowly-scoped coding agent.

Run:
    uv run server.py
    # then in another terminal:
    cloudflared tunnel --url http://localhost:8800

Config via env (all optional, sane defaults for a Mac):
    VOICE_HOST              bind host           (default 127.0.0.1)
    VOICE_PORT             bind port           (default 8800)
    WHISPER_MODEL          faster-whisper size (default base.en)
    WHISPER_DEVICE         cpu|cuda|auto       (default auto)
    WHISPER_COMPUTE        compute type        (default int8)
    LITELLM_BASE_URL       OpenAI-compatible    (default http://localhost:4000)
    LITELLM_MODEL          model/alias name    (default small)
    LITELLM_API_KEY        bearer token        (default "sk-anything")
    VOICE_SYSTEM_PROMPT    system prompt        (has a default)
    TTS_VOICE              macOS `say` voice    (default system voice)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
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

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000").rstrip("/")
LITELLM_MODEL = os.environ.get("LITELLM_MODEL", "small")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "sk-anything")

SYSTEM_PROMPT = os.environ.get(
    "VOICE_SYSTEM_PROMPT",
    "You are a concise voice assistant. The user is speaking to you from their "
    "phone, and your reply will be read aloud, so keep answers short, natural, "
    "and free of markdown, lists, code blocks, or URLs. One or two sentences "
    "unless more is truly needed.",
)

TTS_VOICE = os.environ.get("TTS_VOICE", "").strip()

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
AUDIO_DIR = Path(tempfile.gettempdir()) / "voice-demo-audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Lazy whisper model (load once, on first use)
# ---------------------------------------------------------------------------

_whisper_model = None
_whisper_lock = asyncio.Lock()


async def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        async with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel

                print(
                    f"[whisper] loading model={WHISPER_MODEL} "
                    f"device={WHISPER_DEVICE} compute={WHISPER_COMPUTE} ...",
                    flush=True,
                )
                t0 = time.time()
                _whisper_model = WhisperModel(
                    WHISPER_MODEL,
                    device=WHISPER_DEVICE,
                    compute_type=WHISPER_COMPUTE,
                )
                print(f"[whisper] loaded in {time.time() - t0:.1f}s", flush=True)
    return _whisper_model


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


async def transcribe(audio_path: Path) -> str:
    model = await get_whisper()

    def _run() -> str:
        segments, _info = model.transcribe(str(audio_path), vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    return await asyncio.to_thread(_run)


async def generate_reply(user_text: str) -> str:
    payload = {
        "model": LITELLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.6,
    }
    headers = {"Authorization": f"Bearer {LITELLM_API_KEY}"}
    url = f"{LITELLM_BASE_URL}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"litellm {resp.status_code}: {resp.text[:300]}",
        )
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


async def synthesize(text: str) -> Path:
    """macOS `say` -> .aiff, then afconvert -> .m4a (AAC) for Safari playback."""
    if not shutil.which("say"):
        raise HTTPException(
            status_code=500,
            detail="`say` not found — this server must run on macOS for TTS.",
        )
    uid = uuid.uuid4().hex
    aiff = AUDIO_DIR / f"{uid}.aiff"
    out = AUDIO_DIR / f"{uid}.m4a"

    say_cmd = ["say", "-o", str(aiff)]
    if TTS_VOICE:
        say_cmd += ["-v", TTS_VOICE]
    say_cmd += [text]

    def _run() -> Path:
        subprocess.run(say_cmd, check=True, capture_output=True)
        # Prefer afconvert (always present on macOS) -> AAC/m4a (small, Safari-native).
        if shutil.which("afconvert"):
            subprocess.run(
                ["afconvert", str(aiff), str(out), "-f", "m4af", "-d", "aac"],
                check=True,
                capture_output=True,
            )
            aiff.unlink(missing_ok=True)
            return out
        # Fallback: hand back the raw aiff (Safari can play AIFF too).
        return aiff

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="voice-demo")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "whisper_model": WHISPER_MODEL,
        "whisper_loaded": _whisper_model is not None,
        "litellm_base_url": LITELLM_BASE_URL,
        "litellm_model": LITELLM_MODEL,
        "tts_voice": TTS_VOICE or "(system default)",
        "say_available": bool(shutil.which("say")),
    }


@app.post("/api/talk")
async def talk(audio: UploadFile = File(...)):
    t0 = time.time()
    suffix = Path(audio.filename or "clip.webm").suffix or ".webm"
    tmp = AUDIO_DIR / f"in-{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(await audio.read())

    try:
        transcript = await transcribe(tmp)
        t_asr = time.time()
        if not transcript:
            return JSONResponse(
                {"transcript": "", "reply": "", "audio_url": None,
                 "note": "No speech detected."}
            )

        reply = await generate_reply(transcript)
        t_llm = time.time()

        audio_path = await synthesize(reply)
        t_tts = time.time()

        print(
            f"[turn] asr={t_asr - t0:.1f}s llm={t_llm - t_asr:.1f}s "
            f"tts={t_tts - t_llm:.1f}s total={t_tts - t0:.1f}s | "
            f"q={transcript!r} a={reply!r}",
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


@app.get("/audio/{name}")
async def get_audio(name: str):
    # Guard against path traversal — only serve files we generated.
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

    print(f"[voice-demo] http://{HOST}:{PORT}  (model={WHISPER_MODEL})", flush=True)
    print(
        f"[voice-demo] litellm={LITELLM_BASE_URL} model={LITELLM_MODEL}",
        flush=True,
    )
    if not shutil.which("say"):
        print("[voice-demo] WARNING: `say` not found — TTS will fail off-macOS.",
              file=sys.stderr, flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
