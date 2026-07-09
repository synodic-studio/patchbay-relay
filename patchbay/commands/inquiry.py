"""Shared helper for one-shot harness inquiries (/context, /usage, /compact).

Builds the topic's active harness instance plus a TurnRequest suitable for
capability queries that don't run a full turn. Kept free of `bridge` imports
so it can be imported from any command module without import-order cycles.
"""

from __future__ import annotations

from pathlib import Path

from patchbay.config import MAX_TIMEOUT
from patchbay.efforts import resolve_effort
from patchbay.harness import PiHarness, TurnRequest
from patchbay.models import get_chat_model
from patchbay.projects import get_chat_working_dir
from patchbay.sessions import get_session_id


def resolve_harness_for_inquiry(session_key: str):
    """Return (harness_name, harness, req) for a one-shot inquiry on this topic.

    Only pi is live today; when other harnesses return, this is where their
    instance would be selected per the topic's configured harness.
    """
    from patchbay.config import DEFAULT_HARNESS

    chat_cwd = get_chat_working_dir(session_key)
    session_id = get_session_id(session_key)
    model = get_chat_model(session_key)
    effort = resolve_effort(session_key)

    harness = PiHarness(max_timeout_seconds=MAX_TIMEOUT)

    req = TurnRequest(
        prompt="",
        session_key=session_key,
        project_dir=Path(chat_cwd),
        system_prompt="",
        resume_session_id=session_id,
        model=model,
        effort=effort,
        allowed_tools=None,
        disallowed_tools=None,
        max_turns=None,
        plugin_dir=None,
    )
    return DEFAULT_HARNESS, harness, req
