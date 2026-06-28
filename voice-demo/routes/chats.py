from __future__ import annotations

from fastapi import APIRouter, HTTPException

from chats import _chats, chat_json, create_chat, save_chats

router = APIRouter(prefix="/api/chats")


@router.get("")
async def list_chats():
    ordered = sorted(_chats.values(), key=lambda c: -c.last_active)
    return {"chats": [chat_json(c) for c in ordered]}


@router.post("")
async def new_chat(body: dict):
    try:
        chat = create_chat(body.get("project_dir", "").strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return chat_json(chat)


@router.delete("/{chat_id}")
async def close_chat(chat_id: str):
    if chat_id not in _chats:
        raise HTTPException(status_code=404, detail="Chat not found")
    del _chats[chat_id]
    save_chats()
    return {"ok": True}


@router.post("/{chat_id}/reset")
async def reset_chat(chat_id: str):
    if chat_id not in _chats:
        raise HTTPException(status_code=404, detail="Chat not found")
    _chats[chat_id].pi_session_id = None
    save_chats()
    return chat_json(_chats[chat_id])
