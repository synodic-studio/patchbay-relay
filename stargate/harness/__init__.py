"""Pluggable coding-agent harness layer.

A `Harness` runs one user→agent turn and yields a stream of `TurnEvent`s.
The bridge consumes the stream and produces a Telegram reply. Today there
is one harness (`ClaudeCliHarness`); phase 2 adds `ClaudeSdkHarness`.

See docs/HARNESS-DESIGN.md for the full design.
"""

from .base import (
    Harness,
    HarnessCapabilities,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnError,
    TurnErrorKind,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)
from .aider import AiderHarness
from .claude_cli import ClaudeCliHarness
from .claude_sdk import ClaudeSdkHarness
from .pi import PiHarness

__all__ = [
    "AiderHarness",
    "ClaudeCliHarness",
    "ClaudeSdkHarness",
    "PiHarness",
    "Harness",
    "HarnessCapabilities",
    "TextDelta",
    "ToolResult",
    "ToolUse",
    "TurnError",
    "TurnErrorKind",
    "TurnEvent",
    "TurnFinal",
    "TurnRequest",
]
