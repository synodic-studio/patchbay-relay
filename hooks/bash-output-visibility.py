#!/usr/bin/env python3
"""PostToolUse hook: remind Claude that Bash stdout is invisible to the user.

The Telegram bridge ships Claude's text output to the user, not Bash
stdout. If Claude runs a script that prints a prototype, mockup, table,
or any content it wants the user to see, that content ONLY lands in
Claude's own context — the user sees nothing unless Claude inlines it
in its next text message.

This has bitten us. See the commit message for CTB-80f follow-up:
Claude generated three /usage prototypes to stdout via a Python heredoc,
then wrote "three shapes above" in its reply. User saw only the prose,
no prototypes.

The hook scans Bash stdout for signals that the output was *intentional
display content* (progress bars, banners, decorative dividers, labelled
prototypes/mockups) and fires a system reminder. The heuristic is tight
to avoid false positives on ordinary command output (ls, cat, test runs).

Input: PostToolUse JSON payload on stdin.
Output: either silent (exit 0) or a hookSpecificOutput JSON with
additionalContext. Never blocks.
"""

from __future__ import annotations

import json
import re
import sys

# Minimum stdout size for the heuristic to consider. Short output is
# almost never "display content for the user".
MIN_LINES = 4
MIN_CHARS = 200

# Signals that output was decorative/intentional display content.
# Each match contributes to a hit count; 1+ hits triggers the reminder.
DISPLAY_SIGNALS: list[tuple[str, str]] = [
    (r"[█▓▒░]{3,}", "progress-bar glyphs"),
    (r"[─━═┃║│┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬]{5,}", "Unicode box-drawing runs"),
    (r"^\s*=={3,}\s*$", "`===` divider line"),
    (r"^\s*---{3,}\s*$", "`---` divider line (as display, not diff)"),
    (r"^\s*\*\*\*{2,}\s*$", "`***` divider line"),
    (r"\bPROTOTYPE\s+[A-Z0-9]\b", "labelled PROTOTYPE X header"),
    (r"\bMOCKUP\s+[A-Z0-9]\b", "labelled MOCKUP X header"),
    (r"\bPREVIEW\s+[A-Z0-9]\b", "labelled PREVIEW X header"),
    (r"^\s*```[a-z]*\s*$", "fenced code block marker in output"),
]

COMPILED = [(re.compile(pat, re.MULTILINE), label) for pat, label in DISPLAY_SIGNALS]


def _looks_like_diff(stdout: str) -> bool:
    """Diff/patch output often has --- / +++ / @@ lines; exempt it."""
    head = stdout[:2000]
    return bool(
        re.search(r"^---\s+\S+\n\+\+\+\s+\S+", head, re.MULTILINE)
        or re.search(r"^@@\s+-\d+.*\+\d+", head, re.MULTILINE)
        or re.search(r"^diff --git", head, re.MULTILINE)
    )


def _looks_like_test_output(stdout: str) -> bool:
    """pytest / unittest summaries have their own banner shape we don't
    want to flag — they're not user-facing display content."""
    return bool(
        re.search(r"=+\s*(test session starts|passed|failed|error)", stdout, re.IGNORECASE)
        or "PASSED" in stdout[:200]
        or re.search(r"^\s*Ran \d+ tests? in ", stdout, re.MULTILINE)
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    response = payload.get("tool_response", {}) or {}
    stdout = response.get("stdout") or ""
    if not stdout:
        return 0

    lines = stdout.splitlines()
    if len(lines) < MIN_LINES or len(stdout) < MIN_CHARS:
        return 0

    if _looks_like_diff(stdout) or _looks_like_test_output(stdout):
        return 0

    hits = [label for regex, label in COMPILED if regex.search(stdout)]
    if not hits:
        return 0

    warning = (
        "OUTPUT VISIBILITY: The Bash stdout above contains signals of "
        "display content intended for the user "
        f"({', '.join(hits[:3])}). REMEMBER: Bash stdout lands only in "
        "your context — the user sees NOTHING from it via Telegram. If "
        "you want them to see this content, paste the relevant portion "
        "verbatim into your text response. Never write phrases like "
        "'shown above', 'see output', or 'here's the result' that imply "
        "they can see the tool output."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": warning,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
