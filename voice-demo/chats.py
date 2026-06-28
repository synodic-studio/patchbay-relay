from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import CHATS_FILE, DEVELOPER_DIR


@dataclass
class Chat:
    id: str
    name: str
    project_dir: str
    pi_session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


_chats: dict[str, Chat] = {}


def load_chats() -> None:
    if not CHATS_FILE.exists():
        return
    try:
        raw = json.loads(CHATS_FILE.read_text())
        for c in raw.get("chats", []):
            chat = Chat(**{k: c[k] for k in Chat.__dataclass_fields__ if k in c})
            _chats[chat.id] = chat
    except Exception as exc:
        print(f"[chats] load failed: {exc}", file=sys.stderr)


def save_chats() -> None:
    CHATS_FILE.write_text(json.dumps({"chats": [asdict(c) for c in _chats.values()]}, indent=2))


def chat_json(c: Chat) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "project_dir": c.project_dir,
        "created_at": c.created_at,
        "last_active": c.last_active,
    }


def create_chat(rel: str) -> Chat:
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError("project_dir must be a relative name under DEVELOPER_DIR")
    base = DEVELOPER_DIR.resolve()
    path = (base / rel).resolve()
    if not path.is_dir() or (base not in path.parents and path != base):
        raise ValueError("project_dir must resolve to a directory under DEVELOPER_DIR")
    chat = Chat(id=uuid.uuid4().hex, name=path.name, project_dir=str(path))
    _chats[chat.id] = chat
    save_chats()
    return chat
