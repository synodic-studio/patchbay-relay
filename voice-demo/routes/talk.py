from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

import asr as asr_mod
import tts as tts_mod
from chats import _chats, save_chats
from config import AUDIO_DIR
from pi_runner import run_pi

router = APIRouter()


@router.post("/api/talk")
async def talk(audio: UploadFile = File(...), chat_id: str = Form(...)):
    if chat_id not in _chats:
        raise HTTPException(status_code=404, detail="Chat not found")
    chat = _chats[chat_id]

    t0 = time.time()
    suffix = Path(audio.filename or "clip.webm").suffix or ".webm"
    tmp = AUDIO_DIR / f"in-{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(await audio.read())

    try:
        transcript = await asr_mod.transcribe(tmp)
        t_asr = time.time()

        if not transcript:
            return JSONResponse({"transcript": "", "reply": "", "audio_url": None, "note": "No speech detected."})

        reply = await run_pi(transcript, chat)
        t_llm = time.time()

        audio_path = await tts_mod.synthesize(reply)
        t_tts = time.time()

        chat.last_active = time.time()
        save_chats()

        print(
            f"[turn] {chat.name} asr={t_asr - t0:.1f}s llm={t_llm - t_asr:.1f}s tts={t_tts - t_llm:.1f}s | "
            f"q={transcript!r} a={reply[:80]!r}",
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
