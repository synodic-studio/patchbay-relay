"""Pluggable coding-agent harness layer.

A `Harness` runs one user→agent turn and yields a stream of `TurnEvent`s.
The bridge consumes the stream and produces a Telegram reply.

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
from .claude_cli import ClaudeCliHarness
from .claude_cli import _CAPABILITIES as _CC_CLI_CAPS
from .claude_sdk import ClaudeSdkHarness
from .claude_sdk import _CAPABILITIES as _CC_SDK_CAPS
from .claude_sdk_mop import ClaudeSdkMopHarness
from .claude_sdk_mop import _CAPABILITIES as _CC_SDK_MOP_CAPS
from .pi import PiHarness
from .pi import _CAPABILITIES as _PI_CAPS


CAPABILITIES_BY_NAME: dict[str, HarnessCapabilities] = {
    "cc-cli": _CC_CLI_CAPS,
    "cc-sdk": _CC_SDK_CAPS,
    "cc-sdk-mop": _CC_SDK_MOP_CAPS,
    "pi": _PI_CAPS,
}

__all__ = [
    "CAPABILITIES_BY_NAME",
    "ChannelCapableHarness",
    "ChannelHandle",
    "ClaudeCliHarness",
    "ClaudeSdkHarness",
    "ClaudeSdkMopHarness",
    "CompactCapableHarness",
    "CompactResult",
    "ContextQueryCapableHarness",
    "ContextUsage",
    "Harness",
    "HarnessCapabilities",
    "PiHarness",
    "TextDelta",
    "ToolResult",
    "ToolUse",
    "TurnError",
    "TurnErrorKind",
    "TurnEvent",
    "TurnFinal",
    "TurnRequest",
]
