"""Chat-to-project directory mapping."""

import json
import os
from pathlib import Path

from .config import CHAT_PROJECTS_FILE, WORKING_DIR


def _load_chat_projects() -> dict[str, str]:
    """Load session_key -> relative project path mapping."""
    if CHAT_PROJECTS_FILE.exists():
        try:
            return json.loads(CHAT_PROJECTS_FILE.read_text())
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _save_chat_projects(projects: dict[str, str]) -> None:
    """Write the chat projects mapping to disk."""
    CHAT_PROJECTS_FILE.write_text(json.dumps(projects, indent=2) + "\n")


def _parse_project_entry(entry) -> tuple[str | None, str | None]:
    """Parse a chat_projects entry. Returns (rel_path, agent_name).

    Entries can be:
      - A string: "Fanta" -> ("Fanta", None)
      - An object: {"path": "Fanta", "agent": "iron-temple"} -> ("Fanta", "iron-temple")
    """
    if isinstance(entry, dict):
        return entry.get("path"), entry.get("agent")
    if isinstance(entry, str):
        return entry, None
    return None, None


def get_chat_working_dir(session_key: str) -> str:
    """Resolve working directory for a chat. Returns absolute path."""
    projects = _load_chat_projects()
    entry = projects.get(session_key)
    rel_path, _ = _parse_project_entry(entry)
    if rel_path:
        return os.path.join(WORKING_DIR, rel_path)
    return WORKING_DIR


def get_chat_agent(session_key: str) -> str | None:
    """Return the agent name for this chat, if configured."""
    projects = _load_chat_projects()
    entry = projects.get(session_key)
    _, agent = _parse_project_entry(entry)
    return agent


def set_chat_project(session_key: str, rel_path: str | None) -> None:
    """Set or clear the project directory for a chat."""
    projects = _load_chat_projects()
    if rel_path is None:
        projects.pop(session_key, None)
    else:
        projects[session_key] = rel_path
    _save_chat_projects(projects)


def get_all_projects() -> list[str]:
    """Return all project directory names sorted alphabetically."""
    dev_path = Path(WORKING_DIR)
    dirs = [
        d.name
        for d in dev_path.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    ]
    dirs.sort(key=str.lower)
    return dirs
