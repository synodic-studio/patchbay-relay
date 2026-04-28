#!/usr/bin/env python3
"""PreToolUse hook: warn (never block) when a Bash command is likely to
trigger a macOS TCC / GUI dialog that will hang a headless Claude process.

The user runs Claude headlessly on a Mac Mini via launchd while on a
phone (Telegram bridge). Any command that requires GUI interaction,
Accessibility, Automation, or Screen Recording access will pop a system
dialog nobody can click, and the subprocess sits until the stall detector
reaps it.

The hook reads the PreToolUse JSON payload from stdin, inspects the Bash
command, and if any risky pattern matches, emits a non-blocking warning
to Claude as additionalContext. Exit code is always 0 — we never block.
"""

from __future__ import annotations

import json
import re
import sys

# (regex, human-readable hint). Patterns are case-insensitive.
PATTERNS: list[tuple[str, str]] = [
    (r"xcuitest", "XCUITest needs Accessibility TCC — will hang headless. Use `swift test` instead."),
    (
        r"\bxcodebuild\b.*\btest\b",
        "`xcodebuild test` runs the full test plan including UI tests. Prefer `swift test` or `tuist build`.",
    ),
    (
        r"\btuist\s+test\b",
        "`tuist test` wraps `xcodebuild test`. Prefer `tuist build`; run unit tests via `swift test`.",
    ),
    (r"\bopen\s+-a\b", "`open -a` launches a GUI app — no one is there to interact with it."),
    (
        r"osascript\b.*tell\s+application\s+\"(?!System Events|Finder|Mail|Calendar|Reminders)",
        "`osascript tell application` targeting a GUI app usually hangs on Automation TCC unless pre-approved.",
    ),
    (r"\bxcrun\s+simctl\s+boot\b", "`simctl boot` triggers Simulator UI + Screen Recording prompts."),
    (r"\binstruments\b", "Instruments requires Screen Recording TCC."),
    (r"\bscreencapture\b", "`screencapture` triggers the Screen Recording TCC dialog."),
    (
        # Only match when the find root is the BARE home dir (no subpath).
        # `find ~/Developer/patchbay-relay` is safe; `find ~` cascades into TCC dirs.
        r"\bfind\s+(~|\$HOME|/Users/[\w.-]+)(?=\s|$)",
        "`find ~` / `find $HOME` (bare, no subpath) cascades into macOS "
        "TCC-protected dirs (Photos, Mail, Messages, etc.) and hangs 20+ min "
        "until the stall detector kills it. Use a concrete subdirectory "
        "(e.g. `find ~/.local/bin` or `find ~/Developer`), `which`/`type`, or "
        "a narrow glob. If you truly need recursion from $HOME, add "
        "`-not -path ~/Library -prune` style exclusions.",
    ),
]

COMPILED = [(re.compile(pat, re.IGNORECASE), hint) for pat, hint in PATTERNS]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = payload.get("tool_input", {}).get("command", "")
    if not command:
        return 0

    hits = [hint for regex, hint in COMPILED if regex.search(command)]
    if not hits:
        return 0

    warning = (
        "HEADLESS WARNING: " + " ".join(hits) + " The user is on a phone and cannot click macOS permission dialogs. "
        "If you're not certain this binary is already TCC-approved for the "
        "permissions it needs, pick a headless-safe alternative."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": warning,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
