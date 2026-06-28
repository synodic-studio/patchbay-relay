from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from chats import load_chats
from config import HOST, PORT, PI_MODEL, PI_PROVIDER, CHATS_FILE, STATIC_DIR
from routes.chats import router as chats_router
from routes.misc import router as misc_router
from routes.talk import router as talk_router

app = FastAPI(title="voice-demo")


@app.on_event("startup")
async def startup():
    load_chats()
    from chats import _chats

    print(f"[voice-demo] http://{HOST}:{PORT}  pi={PI_PROVIDER}/{PI_MODEL}", flush=True)
    print(f"[voice-demo] {len(_chats)} chat(s) loaded from {CHATS_FILE}", flush=True)


app.include_router(misc_router)
app.include_router(chats_router)
app.include_router(talk_router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
