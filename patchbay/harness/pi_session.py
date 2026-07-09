"""Read context-window usage from a pi session transcript.

pi stores per-cwd sessions as JSONL under ~/.pi/agent/sessions/<mangled-cwd>/
<timestamp>_<uuid>.jsonl. Each `message` line carries the assistant/user
message, and assistant messages include a real `usage` block
(`input`, `cacheRead`, `cacheWrite`, `totalTokens`, `cost`).

`/context` prefers pi's real token counts when present; when a run's provider
didn't report usage (e.g. some local models leave them at 0), it falls back to
estimating from the transcript text. See ADR/context notes.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import ContextUsage, SessionUsage
from .context_estimate import context_usage_from_count, estimate_context_usage

# pi's default session store (it has no --session-dir override in this bridge).
DEFAULT_PI_SESSIONS_ROOT = Path.home() / ".pi" / "agent" / "sessions"


def find_session_file(sessions_root: Path, session_id: str | None) -> Path | None:
    """Locate the JSONL for *session_id* under any cwd subdir.

    Matches by uuid in the filename so we don't depend on pi's cwd-mangling
    scheme. Returns None when nothing matches.
    """
    if not session_id or not sessions_root.is_dir():
        return None
    hits = sorted(sessions_root.glob(f"*/*{session_id}*.jsonl"))
    return hits[0] if hits else None


def _iter_messages(path: Path):
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "message" and isinstance(obj.get("message"), dict):
            yield obj["message"]


def _real_context_tokens(messages: list[dict]) -> int | None:
    """Real context occupancy from the last assistant message that has usage.

    Context = what was sent to the model on the last turn: input + cache reads
    + cache writes (not output, which isn't part of the next context). Returns
    None when no message carries non-zero token usage.
    """
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        u = m.get("usage") or {}
        ctx = int(u.get("input") or 0) + int(u.get("cacheRead") or 0) + int(u.get("cacheWrite") or 0)
        if ctx > 0:
            return ctx
    return None


def _transcript_text(messages: list[dict]) -> str:
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts)


def context_usage_from_session(path: Path, model: str, *, fallback_window: int = 200_000) -> ContextUsage:
    """Context usage for a pi session file: real tokens if reported, else estimated."""
    messages = list(_iter_messages(path))
    real = _real_context_tokens(messages)
    if real is not None:
        return context_usage_from_count(model, real, fallback_window=fallback_window)
    return estimate_context_usage(model, _transcript_text(messages), fallback_window=fallback_window)


def session_usage(path: Path, model: str | None = None) -> SessionUsage:
    """Sum cost and tokens across a pi session's assistant messages.

    pi records `usage.cost.total` (USD) and token counts per assistant
    message; we sum them for a session total. Tokens may be zero for providers
    that don't report them, in which case only cost is meaningful.
    """
    cost = 0.0
    inp = out = tot = 0
    for m in _iter_messages(path):
        if m.get("role") != "assistant":
            continue
        u = m.get("usage") or {}
        c = (u.get("cost") or {}).get("total")
        if isinstance(c, (int, float)):
            cost += float(c)
        inp += int(u.get("input") or 0)
        out += int(u.get("output") or 0)
        tot += int(u.get("totalTokens") or 0)
    return SessionUsage(
        cost_usd=round(cost, 4),
        input_tokens=inp,
        output_tokens=out,
        total_tokens=tot,
        model=model,
    )
