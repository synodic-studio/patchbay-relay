"""Chat-to-project directory mapping."""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .config import CHAT_PROJECTS_FILE, WORKING_DIR, atomic_write_text, quarantine_file

_PROJECTS_LOCK_FILE = CHAT_PROJECTS_FILE.parent / ".chat_projects.lock"


@contextmanager
def _projects_lock():
    """Acquire exclusive lock for chat_projects read-modify-write cycles."""
    fd = os.open(_PROJECTS_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_chat_projects() -> dict[str, str]:
    """Load session_key -> relative project path mapping. Quarantine on corruption."""
    if not CHAT_PROJECTS_FILE.exists():
        return {}
    try:
        data = json.loads(CHAT_PROJECTS_FILE.read_text())
        if isinstance(data, dict):
            return data
        quarantine_file(CHAT_PROJECTS_FILE, "not a dict")
        return {}
    except (json.JSONDecodeError, TypeError, OSError) as e:
        quarantine_file(CHAT_PROJECTS_FILE, f"corrupt: {e}")
        return {}


def _save_chat_projects(projects: dict[str, str]) -> None:
    """Write the chat projects mapping to disk. Atomic."""
    atomic_write_text(CHAT_PROJECTS_FILE, json.dumps(projects, indent=2) + "\n")


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


def get_chat_harness(session_key: str) -> str | None:
    """Return the per-chat harness name (e.g. "cc-sdk"), or None if unset.

    Stored on dict-form `chat_projects.json` entries under the optional
    "harness" key. String-form entries (legacy) carry no harness override
    and resolve to None — callers fall back to `config.DEFAULT_HARNESS`.
    """
    projects = _load_chat_projects()
    entry = projects.get(session_key)
    if isinstance(entry, dict):
        return entry.get("harness")
    return None


def set_chat_harness(session_key: str, harness: str | None) -> None:
    """Set or clear the per-chat harness override. Uses file locking.

    Promotes a string-form entry to dict-form when needed so the new key
    can land alongside `path` and `agent` without dropping them.
    """
    with _projects_lock():
        projects = _load_chat_projects()
        entry = projects.get(session_key)
        if isinstance(entry, str):
            entry = {"path": entry}
        elif not isinstance(entry, dict):
            entry = {}
        if harness is None:
            entry.pop("harness", None)
        else:
            entry["harness"] = harness
        if entry:
            projects[session_key] = entry
        else:
            projects.pop(session_key, None)
        _save_chat_projects(projects)


def get_chat_title(session_key: str) -> str | None:
    """Return the cached display title for this chat (forum topic name, etc.)."""
    projects = _load_chat_projects()
    entry = projects.get(session_key)
    if isinstance(entry, dict):
        return entry.get("title")
    return None


def set_chat_title(session_key: str, title: str | None) -> None:
    """Cache a display title (forum topic name) for this chat. Uses file locking."""
    with _projects_lock():
        projects = _load_chat_projects()
        entry = projects.get(session_key)
        if isinstance(entry, str):
            entry = {"path": entry}
        elif not isinstance(entry, dict):
            entry = {}
        if title is None:
            entry.pop("title", None)
        else:
            entry["title"] = title
        if entry:
            projects[session_key] = entry
        else:
            projects.pop(session_key, None)
        _save_chat_projects(projects)


def set_chat_project(session_key: str, rel_path: str | None) -> None:
    """Set or clear the project directory for a chat. Uses file locking.

    Preserves other dict-form fields (agent, harness, title) when the
    entry is already a dict — only the path is updated. Clearing removes
    the entry entirely.
    """
    with _projects_lock():
        projects = _load_chat_projects()
        if rel_path is None:
            projects.pop(session_key, None)
        else:
            entry = projects.get(session_key)
            if isinstance(entry, dict):
                entry["path"] = rel_path
                projects[session_key] = entry
            else:
                projects[session_key] = rel_path
        _save_chat_projects(projects)


def get_all_projects() -> list[str]:
    """Return all project directory names sorted alphabetically."""
    dev_path = Path(WORKING_DIR)
    dirs = [d.name for d in dev_path.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))]
    dirs.sort(key=str.lower)
    return dirs
