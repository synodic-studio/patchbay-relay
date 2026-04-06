"""Claude CLI output parsing.

Handles JSON array, NDJSON, and single-object output formats from
`claude --output-format json`. Extracts text, session IDs, and
detects special conditions (max_turns, errors).
"""

import json

from .config import MAX_TURNS, logger
from .sessions import save_session_id


def _parse_events(stdout: str) -> list[dict]:
    """Parse stdout from --output-format json into a list of event dicts.

    Supports:
    - JSON array: [{...}, {...}]
    - Single JSON object: {...}
    - NDJSON: one JSON object per line
    """
    stripped = stdout.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [e for e in parsed if isinstance(e, dict)]
    except (json.JSONDecodeError, TypeError):
        pass
    # Try NDJSON (newline-delimited JSON — one event per line)
    events = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                events.append(obj)
        except (json.JSONDecodeError, TypeError):
            continue
    return events


def _extract_text_from_events(events: list[dict]) -> str | None:
    """Extract text from Claude output events.

    Walks backwards through events to find the last assistant message
    with text content. Falls back to the result event's inline text.
    """
    for e in reversed(events):
        if e.get("type") != "assistant":
            continue
        msg = e.get("message", {})
        content = msg.get("content", []) if isinstance(msg, dict) else []
        texts = [
            c["text"]
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        if texts:
            return "\n".join(texts)

    result_event = next(
        (e for e in reversed(events) if e.get("type") == "result"), None
    )
    if result_event:
        result_text = result_event.get("result")
        if result_text:
            return result_text
        logger.info(
            "Result event found but 'result' field is empty/missing. Keys: %s",
            list(result_event.keys()),
        )

    return None


def parse_claude_response(stdout: str, session_key: str) -> str:
    """Extract text and session_id from claude --output-format json output.

    This is the main entry point for parsing Claude's response. It:
    1. Parses the raw stdout into structured events
    2. Saves the session_id for conversation continuity
    3. Extracts the text response
    4. Handles special cases (max_turns, errors)

    Always returns a non-empty string.
    """
    events = _parse_events(stdout)
    if not events:
        return stdout.strip() or "(no parseable response)"

    event_types: dict[str, int] = {}
    for e in events:
        t = e.get("type", "unknown")
        event_types[t] = event_types.get(t, 0) + 1
    logger.info("Parsed %d events for %s: %s", len(events), session_key, event_types)

    # Save session_id if present
    result_event = next(
        (e for e in reversed(events) if e.get("type") == "result"), None
    )
    if result_event:
        new_session_id = result_event.get("session_id")
        if new_session_id:
            save_session_id(session_key, new_session_id)
            logger.info("Saved session %s for %s", new_session_id[:12], session_key)
        else:
            logger.error(
                "Result event has no session_id for %s — continuity will break",
                session_key,
            )

    text = _extract_text_from_events(events)

    # Detect max_turns and append a notice
    if result_event:
        subtype = result_event.get("subtype") or result_event.get("result_subtype")
        if subtype in ("max_turns", "error_max_turns"):
            notice = f"\n\n[Reached {MAX_TURNS}-turn limit. Session preserved — reply to continue.]"
            if text:
                return text + notice
            # No text produced — try to salvage a summary from what happened
            num_turns = result_event.get("num_turns", "?")
            cost = result_event.get("total_cost_usd", "?")
            return f"(Session used {num_turns} turns / ${cost} but produced no text response. Work may have been done via tools — check the agent's files. Reply to continue.)"
        elif subtype:
            logger.info("Result subtype for %s: %s", session_key, subtype)

    if text:
        return text

    # Check for error info in result event before giving up
    if result_event:
        error = result_event.get("error")
        if error:
            logger.warning("Result event has error for %s: %s", session_key, error)
            return f"(Claude error: {error})"

        # Success but no text — Claude did all work via tools
        subtype = result_event.get("subtype", "")
        if subtype == "success":
            num_turns = result_event.get("num_turns", "?")
            logger.warning(
                "Success with no text for %s (%s turns). Claude likely did work via tools only.",
                session_key, num_turns,
            )
            return f"(Completed {num_turns} turns of work but didn't produce a text response. Check agent files for results.)"

        logger.warning(
            "No text extracted for %s. Result event keys: %s, values preview: %s",
            session_key,
            list(result_event.keys()),
            {k: str(v)[:100] for k, v in result_event.items()},
        )

    return "(no parseable response)"
