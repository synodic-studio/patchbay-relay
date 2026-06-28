from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import HTTPException

from config import AUDIO_DIR, TTS_VOICE


async def synthesize(text: str) -> Path:
    if not shutil.which("say"):
        raise HTTPException(status_code=500, detail="`say` not found — macOS only")
    uid = uuid.uuid4().hex
    aiff = AUDIO_DIR / f"{uid}.aiff"
    out = AUDIO_DIR / f"{uid}.m4a"

    def _run() -> Path:
        subprocess.run(["say", "-v", TTS_VOICE, "-o", str(aiff), text], check=True, capture_output=True)
        if shutil.which("afconvert"):
            subprocess.run(
                ["afconvert", str(aiff), str(out), "-f", "m4af", "-d", "aac"], check=True, capture_output=True
            )
            aiff.unlink(missing_ok=True)
            return out
        return aiff

    return await asyncio.to_thread(_run)
