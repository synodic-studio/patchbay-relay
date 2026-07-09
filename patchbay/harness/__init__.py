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
    SessionUsage,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnError,
    TurnErrorKind,
    TurnEvent,
    TurnFinal,
    TurnRequest,
    UsageQueryCapableHarness,
)
from .pi import PiHarness
from .pi import _CAPABILITIES as _PI_CAPS


CAPABILITIES_BY_NAME: dict[str, HarnessCapabilities] = {
    "pi": _PI_CAPS,
}

__all__ = [
    "CAPABILITIES_BY_NAME",
    "ChannelCapableHarness",
    "ChannelHandle",
    "CompactCapableHarness",
    "CompactResult",
    "ContextQueryCapableHarness",
    "ContextUsage",
    "Harness",
    "HarnessCapabilities",
    "PiHarness",
    "SessionUsage",
    "TextDelta",
    "ToolResult",
    "ToolUse",
    "TurnError",
    "TurnErrorKind",
    "TurnEvent",
    "TurnFinal",
    "TurnRequest",
    "UsageQueryCapableHarness",
]
