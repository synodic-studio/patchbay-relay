#!/usr/bin/env python3
"""MOP stop hook — blocks agent from stopping if it sent no text this turn.

Wired as a Claude Code Stop hook. Reads the session transcript and checks
whether the agent's most recent assistant turn contains any text content.
If it produced only tool calls with no text, blocks stopping and injects
a reminder to send at least one message.

Wire in .claude/settings.json (agent project dir) or ~/.claude/settings.json:
    {
      "hooks": {
        "Stop": [{
          "matcher": "",
          "hooks": [{"type": "command", "command": "python3 /path/to/mop_stop_hook.py"}]
        }]
      }
    }

Input: JSON on stdin — {"session_id": "...", "transcript_path": "...", "type": "stop"}
Output: nothing (exit 0) = allow stop
        {"decision": "block", "reason": "..."} on stdout + exit 0 = block stop
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _has_text_content(transcript_path: str) -> bool:
    """Return True if the last assistant message in the transcript has text content."""
    path = Path(transcript_path)
    if not path.exists():
        return True  # can't verify; allow stopping

    last_assistant_text = ""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Claude Code NDJSON format: {"type": "assistant", "message": {...}}
            if msg.get("type") == "assistant":
                content = msg.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_assistant_text = block.get("text", "")
                elif isinstance(content, str):
                    last_assistant_text = content
    except Exception:
        return True  # parse error; allow stopping

    return bool(last_assistant_text.strip())


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)  # bad input; allow stopping

    transcript_path = data.get("transcript_path", "")

    if not _has_text_content(transcript_path):
        result = {
            "decision": "block",
            "reason": (
                "MOP: your turn produced no text message. "
                "You must send at least one text reply before stopping."
            ),
        }
        print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()
