"""Pluggable coding-agent harness layer.

A `Harness` runs one user→agent turn and yields a stream of `TurnEvent`s.
The bridge consumes the stream and produces a Telegram reply. Today there
is one harness (`ClaudeCliHarness`); phase 2 adds `ClaudeSdkHarness`.

See docs/HARNESS-DESIGN.md for the full design.
"""

from .base import (
    ChannelCapableHarness,
    ChannelHandle,
    CompactCapableHarness,
    CompactResult,
    ContextQueryCapableHarness,
    ContextUsage,
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
from .aider import _CAPABILITIES as _AIDER_CAPS
from .claude_cli import ClaudeCliHarness
from .claude_cli import _CAPABILITIES as _CC_CLI_CAPS
from .claude_sdk import ClaudeSdkHarness
from .claude_sdk import _CAPABILITIES as _CC_SDK_CAPS
from .opencode import OpenCodeHarness
from .opencode import _CAPABILITIES as _OPENCODE_CAPS
from .pi import PiHarness
from .pi import _CAPABILITIES as _PI_CAPS


CAPABILITIES_BY_NAME: dict[str, HarnessCapabilities] = {
    "cc-cli": _CC_CLI_CAPS,
    "cc-sdk": _CC_SDK_CAPS,
    "pi": _PI_CAPS,
    "aider": _AIDER_CAPS,
    "opencode": _OPENCODE_CAPS,
}

__all__ = [
    "AiderHarness",
    "CAPABILITIES_BY_NAME",
    "ChannelCapableHarness",
    "ChannelHandle",
    "ClaudeCliHarness",
    "CompactCapableHarness",
    "CompactResult",
    "ContextQueryCapableHarness",
    "ContextUsage",
    "ClaudeSdkHarness",
    "OpenCodeHarness",
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
