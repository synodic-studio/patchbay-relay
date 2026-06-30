from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import HTTPException

from config import AUDIO_DIR, GOOGLE_TTS_VOICE, TTS_PROVIDER, TTS_VOICE

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    return parts if parts else [text]


async def synthesize(text: str, provider: str | None = None, chunked: bool = False) -> list[Path]:
    effective = provider or TTS_PROVIDER
    chunks = _split_sentences(text) if chunked else [text]
    tasks = [_synthesize_one(chunk, effective) for chunk in chunks]
    return await asyncio.gather(*tasks)


async def _synthesize_one(text: str, provider: str) -> Path:
    if provider == "google":
        return await _google_tts(text)
    return await _say_tts(text)


async def _say_tts(text: str) -> Path:
    if not shutil.which("say"):
        raise HTTPException(status_code=500, detail="`say` not found — macOS only")
    uid = uuid.uuid4().hex
    aiff = AUDIO_DIR / f"{uid}.aiff"
    out = AUDIO_DIR / f"{uid}.m4a"

    def _run() -> Path:
        subprocess.run(["say", "-v", TTS_VOICE, "-o", str(aiff), text], check=True, capture_output=True)
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


async def _google_tts(text: str) -> Path:
    try:
        from google.cloud import texttospeech
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="google-cloud-texttospeech not installed; add it to server.py script deps",
        )

    uid = uuid.uuid4().hex
    out = AUDIO_DIR / f"{uid}.mp3"

    def _run() -> Path:
        client = texttospeech.TextToSpeechClient()
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code="en-US",
                name=GOOGLE_TTS_VOICE,
            ),
            audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3),
        )
        out.write_bytes(response.audio_content)
        return out

    return await asyncio.to_thread(_run)
