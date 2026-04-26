"""Harness protocol + event types.

A harness runs one turn (user prompt → agent response) and yields a stream
of `TurnEvent`s. The stream always terminates with exactly one `TurnFinal`
or `TurnError`. Other events (`TextDelta`, `ToolUse`, `ToolResult`) may
appear any number of times before the terminator.

The contract is small on purpose. It captures what `bridge.run_claude`
actually needs from the underlying agent today (text, tool calls,
session_id, classified failures) without baking in CLI-specific or
SDK-specific shapes. Future harnesses (codex, cursor, …) advertise what
they support via `HarnessCapabilities`; the bridge branches on that.

See docs/HARNESS-DESIGN.md for design rationale.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, Union, runtime_checkable

# Failure classes the bridge knows how to act on. Strings match
# stargate.self_heal repair-handler keys where possible so dispatch is
# uniform across CLI and SDK harnesses.
TurnErrorKind = Literal[
    "rate_limit",       # quota or rate limit; bridge does Forge handoff
    "oom",              # OOM-shaped exit (137 / -9); bridge retries with trimmed prompt
    "corrupt_session",  # session storage couldn't be read; quarantine + fresh start
    "max_turns",        # hit the turn budget; bridge appends a notice
    "timeout",          # wall-clock timeout for one turn
    "process_died",     # subprocess crashed mid-turn (no other classification fits)
    "unknown",          # something failed; the message is all we know
]


@dataclass(frozen=True)
class HarnessCapabilities:
    """What the underlying agent supports.

    The bridge reads these to decide whether to pass `resume_session_id`,
    expect mid-turn `ToolUse` events, etc. Non-Claude harnesses (codex,
    cursor) will have several flags False.
    """

    supports_resume: bool          # can resume a previous session by id
    supports_tool_streaming: bool  # emits ToolUse events mid-turn (vs. only post-hoc)
    supports_interrupt: bool       # cancel() works without SIGKILL
    supports_effort: bool          # honors low/medium/high/max effort
    supports_mcp: bool             # can load MCP servers
    supports_inflight_push: bool = False    # accepts new user messages mid-turn (channels)
    supports_context_query: bool = False    # can report current token usage
    supports_compact: bool = False          # can compact / summarize the running context


@dataclass(frozen=True)
class TurnRequest:
    """One turn's worth of input.

    Stargate builds this in the bridge per message and hands it to the
    selected harness. Fields the harness doesn't support are silently
    ignored (e.g. a codex harness ignores `plugin_dir`).
    """

    prompt: str
    session_key: str               # stargate's chat:thread key (for logging only)
    project_dir: Path
    system_prompt: str
    resume_session_id: str | None  # None → fresh session
    model: str | None              # None → harness default
    effort: str | None             # None → harness default
    allowed_tools: list[str] | None
    disallowed_tools: list[str] | None
    max_turns: int | None
    plugin_dir: str | None         # claude-specific; harnesses may ignore
    extra: dict | None = None      # harness-specific knobs (e.g. {"output_format": "json"})


# ---- TurnEvent: tagged union streamed back from the harness ----


@dataclass(frozen=True)
class TextDelta:
    """Incremental assistant text. `final=True` marks the end of the
    assistant's text stream — but it's NOT the terminator of the turn;
    a `TurnFinal` or `TurnError` still follows."""

    text: str
    final: bool = False


@dataclass(frozen=True)
class ToolUse:
    name: str
    input: dict
    id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str | None
    output: str
    is_error: bool = False


@dataclass(frozen=True)
class TurnError:
    """A failure the harness has classified. `retryable` indicates whether
    the bridge can call `run_turn` again with the same request (after
    optional cooldown / repair); `metadata` carries harness-specific
    extras like `exit_code` or `stderr` for activity logging."""

    kind: TurnErrorKind
    message: str
    retryable: bool
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TurnFinal:
    """Last event of a successful turn. `session_id` is the harness's
    new/refreshed conversation id (None if this harness can't resume).
    `raw_text` is the full assistant text the bridge will send to
    Telegram — already aggregated, ready to use."""

    session_id: str | None
    num_turns: int | None
    total_cost_usd: float | None
    raw_text: str


TurnEvent = Union[TextDelta, ToolUse, ToolResult, TurnError, TurnFinal]


@runtime_checkable
class Harness(Protocol):
    """A coding-agent backend.

    Implementations: `ClaudeCliHarness`, `ClaudeSdkHarness` (phase 2),
    future codex/cursor harnesses.

    Contract:
    - `run_turn` is an async generator yielding `TurnEvent`s.
    - The stream ends with **exactly one** `TurnFinal` *or* `TurnError`.
    - Any number of other events may precede the terminator.
    - Implementations should not raise from `run_turn`; classified
      failures go through `TurnError`. Catastrophic protocol bugs (e.g.
      a missing terminator) are the only exception.
    """

    name: str
    capabilities: HarnessCapabilities

    def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Yield events for one turn."""
        ...

    async def cancel(self) -> None:
        """Best-effort cancel of the in-flight turn (if any). Idempotent.
        /kill calls this. May SIGKILL on harnesses without graceful
        cancellation (`capabilities.supports_interrupt=False`)."""
        ...


@runtime_checkable
class ChannelCapableHarness(Protocol):
    """Optional secondary protocol for harnesses that support channels.

    Bridge calls `isinstance(h, ChannelCapableHarness)` (or, equivalently,
    checks `h.capabilities.supports_inflight_push`) before invoking
    `open_channel`. Keeping this off the base Harness Protocol means
    legacy harnesses don't have to add a no-op stub to satisfy structural
    typing.
    """

    capabilities: HarnessCapabilities

    async def open_channel(self, req: TurnRequest) -> "ChannelHandle":
        """Open a long-lived conversation supporting mid-flight pushes.

        Only callable when `capabilities.supports_inflight_push` is True.
        """
        ...


@dataclass(frozen=True)
class ContextUsage:
    """Snapshot of token usage for one session.

    Returned by `ContextQueryCapableHarness.get_context()`. Only the
    totals are required; details are optional and omitted by harnesses
    that can't report them.
    """

    used_tokens: int
    max_tokens: int
    percentage: float           # 0.0 to 100.0
    model: str | None = None    # model whose context window is being measured


@dataclass(frozen=True)
class CompactResult:
    """Outcome of a `compact()` call."""

    succeeded: bool
    message: str                # human-readable status (e.g. "Context compacted")
    tokens_before: int | None = None
    tokens_after: int | None = None


@runtime_checkable
class ContextQueryCapableHarness(Protocol):
    """Optional secondary protocol for harnesses that can report token usage."""

    capabilities: HarnessCapabilities

    async def get_context(self, req: TurnRequest) -> ContextUsage:
        """Report current context-window usage for this session.

        `req` carries the session/cwd/model needed to resolve which
        underlying agent state to query. Implementations may open a
        transient subprocess if no live channel is held.
        """
        ...


@runtime_checkable
class CompactCapableHarness(Protocol):
    """Optional secondary protocol for harnesses that can compact context."""

    capabilities: HarnessCapabilities

    async def compact(
        self, req: TurnRequest, instructions: str | None = None
    ) -> CompactResult:
        """Compact the running context, optionally steered by `instructions`.

        Mirrors the user-facing /compact slash command in claude code:
        runs the agent's compaction routine on the current session,
        returning a brief result the bridge can show in Telegram.
        """
        ...


@runtime_checkable
class ChannelHandle(Protocol):
    """A long-lived agent conversation that accepts mid-flight messages.

    Returned by `Harness.open_channel(req)` for harnesses where
    `capabilities.supports_inflight_push` is True.

    Lifecycle:
    - `events()` yields `TurnEvent`s for the entire conversation, across
      multiple user messages. The stream stays open until `close()`.
    - Each call to `push(prompt)` enqueues a new user message. The model
      processes them in order at its next decision point. There is no
      one-to-one mapping between `push` calls and `TurnFinal` events;
      the agent may emit a TurnFinal between turns.
    - `interrupt()` cancels the in-flight turn but leaves the channel
      open for further pushes.
    - `close()` is idempotent; events() ends shortly after.

    `session_id` is populated as soon as the underlying agent surfaces
    one (typically after the first model response). Callers may read it
    at any time but should expect None until then.
    """

    session_id: str | None

    async def push(self, prompt: str) -> None:
        """Enqueue a new user message into the open conversation."""
        ...

    def events(self) -> AsyncIterator[TurnEvent]:
        """Async iterator over events for the lifetime of the channel."""
        ...

    async def interrupt(self) -> None:
        """Cancel the in-flight turn. Channel stays open."""
        ...

    async def close(self) -> None:
        """Close the channel and underlying transport. Idempotent."""
        ...


def is_terminator(event: TurnEvent) -> bool:
    """True if `event` is `TurnFinal` or `TurnError` (i.e. ends the stream)."""
    return isinstance(event, (TurnFinal, TurnError))
