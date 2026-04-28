"""ClaudeSdkChannel — long-lived ClaudeSDKClient wrapped as a ChannelHandle.

The cc-sdk harness uses this for inflight push channels: the bridge
holds a single client open across multiple user messages, calling
`push()` on each new Telegram message instead of spawning a new turn.

Lifecycle:
- `open(initial_prompt)` — connect the underlying ClaudeSDKClient and
  enqueue the first user message. Spawns a background task that drains
  `client.receive_messages()` and converts each SDK Message into a
  `TurnEvent` on an internal queue.
- `push(prompt)` — call `client.query(prompt)` to inject a new user
  message. The model picks it up at its next decision point.
- `events()` — async iterator yielding TurnEvents as they arrive.
- `interrupt()` — `client.interrupt()`. Channel stays open.
- `close()` — cancel drain task, disconnect client, end events().

All async operations execute on the worker thread's event loop. The
bridge schedules push/interrupt/close via run_coroutine_threadsafe
when called from the main asyncio loop.

The SDK constraint (cannot cross async runtime contexts) is preserved
because the entire channel lives in one event loop's task group.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from ..config import logger
from .base import (
    ChannelHandle,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnError,
    TurnEvent,
    TurnFinal,
)


# Sentinel pushed to _event_queue to signal end-of-stream.
_END_SENTINEL = object()


class ClaudeSdkChannel:
    """A ChannelHandle backed by a long-lived ClaudeSDKClient.

    Constructed by `ClaudeSdkHarness.open_channel()`. Not used directly
    by the bridge — the bridge sees a `ChannelHandle` (Protocol).
    """

    def __init__(
        self,
        *,
        options,                                  # ClaudeAgentOptions
        on_progress: Callable[[], None] | None = None,
    ) -> None:
        self._options = options
        self._on_progress = on_progress
        self._client = None
        self._drain_task: asyncio.Task | None = None
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._opened = False
        self._closed = False
        # Captured from the first SystemMessage (init) or every
        # ResultMessage. Surfaces to the bridge via the public attribute.
        self.session_id: str | None = None

    # ---- ChannelHandle protocol ----

    async def push(self, prompt: str) -> None:
        if self._closed:
            raise RuntimeError("ClaudeSdkChannel is closed")
        if not self._opened or self._client is None:
            raise RuntimeError("ClaudeSdkChannel.open() not called")
        await self._client.query(prompt)

    async def interrupt(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception:  # noqa: BLE001
            logger.exception("ClaudeSdkChannel.interrupt failed")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("ClaudeSdkChannel.close: disconnect raised")
        # Make sure events() unblocks even if nothing else pushed the sentinel.
        try:
            self._event_queue.put_nowait(_END_SENTINEL)
        except asyncio.QueueFull:  # pragma: no cover — unbounded queue
            pass

    async def events(self) -> AsyncIterator[TurnEvent]:
        while True:
            item = await self._event_queue.get()
            if item is _END_SENTINEL:
                return
            yield item

    # ---- Lifecycle ----

    async def open(self, initial_prompt: str) -> None:
        """Connect the SDK client and start draining. Idempotent within
        a single instance — calling open() twice is a programming error."""
        if self._opened:
            raise RuntimeError("ClaudeSdkChannel already opened")
        # Imported lazily so this module is importable without the SDK.
        from claude_agent_sdk import ClaudeSDKClient

        self._client = ClaudeSDKClient(options=self._options)
        await self._client.connect()
        await self._client.query(initial_prompt)
        self._opened = True
        self._drain_task = asyncio.create_task(self._drain())

    # ---- Internals ----

    async def _drain(self) -> None:
        """Pull messages from the SDK client, convert, push to queue.

        Runs as a background task for the lifetime of the channel.
        Cancellation (from close()) is the normal exit. Any unexpected
        exception is surfaced as a TurnError on the queue so callers
        see it.
        """
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                CLIConnectionError,
                CLIJSONDecodeError,
                CLINotFoundError,
                ProcessError,
                ResultMessage,
                SystemMessage,
                TextBlock,
                ToolResultBlock,
                ToolUseBlock,
                UserMessage,
            )

            current_text_chunks: list[str] = []

            async for msg in self._client.receive_messages():
                if self._on_progress is not None:
                    try:
                        self._on_progress()
                    except Exception:  # noqa: BLE001
                        logger.exception("on_progress callback raised")

                if isinstance(msg, AssistantMessage):
                    if msg.error == "rate_limit":
                        await self._event_queue.put(
                            TurnError(
                                kind="rate_limit",
                                message="Rate limit reported by SDK",
                                retryable=False,
                                metadata={"sdk_error": msg.error},
                            )
                        )
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            current_text_chunks.append(block.text)
                            await self._event_queue.put(TextDelta(text=block.text))
                        elif isinstance(block, ToolUseBlock):
                            await self._event_queue.put(
                                ToolUse(
                                    name=block.name,
                                    input=dict(block.input),
                                    id=block.id,
                                )
                            )
                elif isinstance(msg, UserMessage):
                    if isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                await self._event_queue.put(
                                    ToolResult(
                                        tool_use_id=block.tool_use_id,
                                        output=_stringify_tool_output(block.content),
                                        is_error=bool(block.is_error),
                                    )
                                )
                elif isinstance(msg, SystemMessage):
                    sid = msg.data.get("session_id") if isinstance(msg.data, dict) else None
                    if sid:
                        self.session_id = sid
                elif isinstance(msg, ResultMessage):
                    self.session_id = msg.session_id or self.session_id
                    full_text = "".join(current_text_chunks) or msg.result or ""
                    await self._event_queue.put(
                        _terminator_from_result(msg, full_text)
                    )
                    # Reset text accumulator for the next turn on this channel.
                    current_text_chunks = []
        except asyncio.CancelledError:
            return
        except CLINotFoundError as e:
            await self._event_queue.put(
                TurnError(
                    kind="process_died",
                    message=f"Claude CLI not found: {e}",
                    retryable=False,
                    metadata={"sdk_error": "CLINotFoundError"},
                )
            )
        except CLIConnectionError as e:
            await self._event_queue.put(
                TurnError(
                    kind="process_died",
                    message=f"Connection to claude CLI lost: {e}",
                    retryable=True,
                    metadata={"sdk_error": "CLIConnectionError"},
                )
            )
        except ProcessError as e:
            exit_code = getattr(e, "exit_code", None)
            stderr = getattr(e, "stderr", "") or ""
            kind = "oom" if exit_code in (137, -9) else "process_died"
            await self._event_queue.put(
                TurnError(
                    kind=kind,
                    message=f"CLI subprocess failed (rc={exit_code}): {stderr[:200]}",
                    retryable=True,
                    metadata={"exit_code": exit_code, "stderr": stderr[:200]},
                )
            )
        except CLIJSONDecodeError as e:
            await self._event_queue.put(
                TurnError(
                    kind="corrupt_session",
                    message=f"SDK could not parse CLI output: {e}",
                    retryable=True,
                    metadata={
                        "sdk_error": "CLIJSONDecodeError",
                        "line": getattr(e, "line", "")[:200],
                    },
                )
            )
        except Exception as e:  # noqa: BLE001 — channel must surface, not crash
            logger.exception("ClaudeSdkChannel._drain unexpected error")
            await self._event_queue.put(
                TurnError(
                    kind="unknown",
                    message=f"Channel drain error: {e}",
                    retryable=False,
                    metadata={"exception": type(e).__name__},
                )
            )
        finally:
            await self._event_queue.put(_END_SENTINEL)


def _terminator_from_result(msg, full_text: str) -> TurnEvent:
    """Map a ResultMessage to a TurnFinal or TurnError. Mirrors
    ClaudeSdkHarness._terminator_from_result with the same classification
    rules so channel turns and one-shot turns are observably equivalent."""
    subtype = msg.subtype or ""
    if subtype in ("max_turns", "error_max_turns"):
        return TurnError(
            kind="max_turns",
            message=full_text or f"Reached {msg.num_turns}-turn limit",
            retryable=False,
            metadata={
                "session_id": msg.session_id,
                "num_turns": msg.num_turns,
                "total_cost_usd": msg.total_cost_usd,
                "subtype": subtype,
            },
        )
    if msg.is_error:
        errors = list(msg.errors or [])
        joined = " ".join(errors)
        kind = "rate_limit" if any(
            tok in joined.lower()
            for tok in ("rate limit", "quota", " 429 ", "too many requests")
        ) else "unknown"
        return TurnError(
            kind=kind,
            message=joined or f"SDK reported error (subtype={subtype})",
            retryable=(kind == "rate_limit"),
            metadata={
                "session_id": msg.session_id,
                "subtype": subtype,
                "errors": errors,
            },
        )
    return TurnFinal(
        session_id=msg.session_id,
        num_turns=msg.num_turns,
        total_cost_usd=msg.total_cost_usd,
        raw_text=full_text or "(no parseable response)",
    )


def _stringify_tool_output(content) -> str:
    """ToolResultBlock.content is str | list[dict] | None — reduce to str."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content") or ""
                parts.append(str(t))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


# Type-check that we satisfy the ChannelHandle protocol at import time.
def _protocol_check_factory(client_options) -> ChannelHandle:
    return ClaudeSdkChannel(options=client_options)


__all__ = ["ClaudeSdkChannel"]
