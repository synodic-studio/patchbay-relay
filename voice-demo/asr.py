from __future__ import annotations

import asyncio
import time
from pathlib import Path

from config import WHISPER_COMPUTE, WHISPER_DEVICE, WHISPER_INITIAL_PROMPT, WHISPER_MODEL

_model = None
_lock = asyncio.Lock()


def is_loaded() -> bool:
    return _model is not None


async def _get_model():
    global _model
    if _model is None:
        async with _lock:
            if _model is None:
                from faster_whisper import WhisperModel

                print(f"[whisper] loading {WHISPER_MODEL} ...", flush=True)
                t0 = time.time()
                _model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
                print(f"[whisper] loaded in {time.time() - t0:.1f}s", flush=True)
    return _model


async def transcribe(audio_path: Path) -> str:
    model = await _get_model()

    def _run() -> str:
        segments, _ = model.transcribe(str(audio_path), vad_filter=True, initial_prompt=WHISPER_INITIAL_PROMPT)
        return " ".join(seg.text.strip() for seg in segments).strip()

    return await asyncio.to_thread(_run)
