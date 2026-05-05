"""ClaudeSdkHarness — runs a turn via the Claude Agent SDK.

The SDK still uses the `claude` CLI as a subprocess under the hood, so
the blast-radius story is the same as `ClaudeCliHarness`. What the SDK
buys us is typed messages instead of raw NDJSON parsing: AssistantMessage
content blocks already separate text/tool_use, ResultMessage carries
session_id/cost as fields, AssistantMessage.error is a typed Literal
with a `"rate_limit"` value (no string matching for the common case).

Sits as a peer to ClaudeCliHarness. Tests below stub the SDK client
so they don't burn API credits.

See docs/HARNESS-DESIGN.md for the protocol contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from ..config import CLAUDE_PATH, MAX_TIMEOUT, MAX_TURNS, logger
from ..quota import is_quota_error
from .base import (
    CompactResult,
    ContextUsage,
    Harness,
    HarnessCapabilities,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)


_CAPABILITIES = HarnessCapabilities(
    supports_resume=True,
    supports_tool_streaming=True,   # SDK streams content blocks per turn
    supports_interrupt=True,        # task cancellation propagates to subprocess
    supports_effort=True,
    supports_mcp=True,
    supports_inflight_push=True,    # client.query() injects mid-conversation
    supports_context_query=True,    # client.get_context_usage()
    supports_compact=True,          # /compact slash command via client.query()
)


class ClaudeSdkHarness:
    """Run a turn via `claude_agent_sdk.ClaudeSDKClient`."""

    name = "cc-sdk"
    capabilities = _CAPABILITIES

    def __init__(
        self,
        *,
        cli_path: str = CLAUDE_PATH,
        max_timeout_seconds: float = MAX_TIMEOUT,
        max_turns_default: int = MAX_TURNS,
        on_progress: Callable[[], None] | None = None,
    ) -> None:
        self._cli_path = cli_path
        self._max_timeout = max_timeout_seconds
        self._max_turns_default = max_turns_default
        # External observer (the bridge) calls this on every SDK message
        # so SessionState.last_event_at advances and the stall detector
        # has the same per-event cadence signal it gets from cc-cli.
        self._on_progress = on_progress
        # The current in-flight task, so cancel() can interrupt cleanly.
        self._task: asyncio.Task | None = None

    # ---- Public API ----

    async def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]:
        """See Harness.run_turn. Always yields exactly one TurnFinal or TurnError last."""
        # Imported lazily so this module is importable in environments where
        # claude-agent-sdk isn't installed (the CLI harness has no such dep).
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
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

        options = self._build_options(req, ClaudeAgentOptions)

        # Capture the running task so cancel() can interrupt it. Without
        # this, the bridge's stall detector and `/kill` command appear to
        # cancel the turn (logging + user notification fire) but the SDK
        # task keeps running unfettered — the user sees a "killed" message
        # AND a successful response moments later. Set on entry, cleared
        # in the finally block so a subsequent turn doesn't inherit a
        # stale handle.
        self._task = asyncio.current_task()

        text_chunks: list[str] = []
        captured_session_id: str | None = None

        try:
            async with asyncio.timeout(self._max_timeout):
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(req.prompt)
                    async for msg in client.receive_response():
                        # Refresh the bridge's stall-detector timestamp on
                        # every SDK message — same cadence guarantee as
                        # cc-cli's per-stdout-line `on_progress` callback.
                        if self._on_progress is not None:
                            try:
                                self._on_progress()
                            except Exception:  # noqa: BLE001 — never let a callback bring us down
                                logger.exception("on_progress callback raised")
                        if isinstance(msg, AssistantMessage):
                            # AssistantMessage.error is a typed Literal — much
                            # cleaner than string-matching stderr.
                            if msg.error == "rate_limit":
                                yield TurnError(
                                    kind="rate_limit",
                                    message="Rate limit reported by SDK",
                                    retryable=False,
                                    metadata={"sdk_error": msg.error},
                                )
                                return
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    text_chunks.append(block.text)
                                    yield TextDelta(text=block.text)
                                elif isinstance(block, ToolUseBlock):
                                    yield ToolUse(
                                        name=block.name,
                                        input=dict(block.input),
                                        id=block.id,
                                    )
                                # ThinkingBlock / ServerToolUseBlock /
                                # ServerToolResultBlock pass silently.
                        elif isinstance(msg, UserMessage):
                            # The SDK sends tool results back as a UserMessage
                            # with ToolResultBlock content. Surface them so
                            # the bridge's activity log can record what tools
                            # actually returned.
                            if isinstance(msg.content, list):
                                for block in msg.content:
                                    if isinstance(block, ToolResultBlock):
                                        yield ToolResult(
                                            tool_use_id=block.tool_use_id,
                                            output=_stringify_tool_output(block.content),
                                            is_error=bool(block.is_error),
                                        )
                        elif isinstance(msg, SystemMessage):
                            # init / config events. Capture session_id as a
                            # backstop in case ResultMessage doesn't arrive.
                            sid = msg.data.get("session_id") if isinstance(msg.data, dict) else None
                            if sid:
                                captured_session_id = sid
                        elif isinstance(msg, ResultMessage):
                            yield self._terminator_from_result(msg, text_chunks)
                            return
        except asyncio.TimeoutError:
            yield TurnError(
                kind="timeout",
                message=f"SDK turn timed out after {self._max_timeout / 60:.0f} min",
                retryable=True,
                metadata={"max_timeout_seconds": self._max_timeout},
            )
            return
        except CLINotFoundError as e:
            yield TurnError(
                kind="process_died",
                message=f"Claude CLI not found: {e}",
                retryable=False,
                metadata={"sdk_error": "CLINotFoundError"},
            )
            return
        except CLIConnectionError as e:
            yield TurnError(
                kind="process_died",
                message=f"Connection to claude CLI lost: {e}",
                retryable=True,
                metadata={"sdk_error": "CLIConnectionError"},
            )
            return
        except ProcessError as e:
            yield self._error_from_process_error(e)
            return
        except CLIJSONDecodeError as e:
            yield TurnError(
                kind="corrupt_session",
                message=f"SDK could not parse CLI output: {e}",
                retryable=True,
                metadata={"sdk_error": "CLIJSONDecodeError", "line": getattr(e, "line", "")[:200]},
            )
            return
        finally:
            # Always clear the task handle — a stale handle on a subsequent
            # turn would let cancel() target an already-finished task and
            # leak the new one (the bug the assignment in run_turn fixes).
            self._task = None

        # Stream ended without ResultMessage. This is a contract violation
        # by the SDK, but we still need to terminate the stream cleanly.
        logger.warning(
            "SDK stream ended without ResultMessage for %s — emitting fallback TurnFinal",
            req.session_key,
        )
        yield TurnFinal(
            session_id=captured_session_id,
            num_turns=None,
            total_cost_usd=None,
            raw_text="".join(text_chunks) or "(no parseable response)",
        )

    async def cancel(self) -> None:
        """Cancel the active turn task. Idempotent."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — silence everything during cancel
            pass

    async def open_channel(self, req: TurnRequest):
        """Open a long-lived ClaudeSdkChannel for inflight pushes.

        Returns a `ChannelHandle` that wraps a persistent ClaudeSDKClient.
        The bridge calls this when `capabilities.supports_inflight_push`
        is True and no channel is currently held for the session_key.
        """
        from claude_agent_sdk import ClaudeAgentOptions

        from .claude_sdk_channel import ClaudeSdkChannel

        options = self._build_options(req, ClaudeAgentOptions)
        channel = ClaudeSdkChannel(options=options, on_progress=self._on_progress)
        await channel.open(req.prompt)
        return channel

    async def get_context(self, req: TurnRequest) -> ContextUsage:
        """Open a transient client and read its context-window usage.

        For `/context` from the bridge. Cheap-ish — opens a fresh client
        that resumes the session (if `req.resume_session_id` is set) and
        immediately calls `get_context_usage()` before disconnecting.
        Captured totals match what the CLI's `/context` would show.
        """
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        options = self._build_options(req, ClaudeAgentOptions)
        async with ClaudeSDKClient(options=options) as client:
            usage = await client.get_context_usage()
        return ContextUsage(
            used_tokens=int(usage.get("totalTokens", 0)),
            max_tokens=int(usage.get("rawMaxTokens") or usage.get("maxTokens", 0)),
            percentage=float(usage.get("percentage", 0)),
            model=usage.get("model"),
        )

    async def compact(
        self, req: TurnRequest, instructions: str | None = None
    ) -> CompactResult:
        """Trigger claude's `/compact` slash command on the live session.

        We open a transient client that resumes the session, sample the
        context size, push `/compact` (with optional steering text) as
        a user message, drain the response, then re-sample. Returns a
        CompactResult the bridge can show to the user.
        """
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
        )

        options = self._build_options(req, ClaudeAgentOptions)
        prompt = "/compact" if not instructions else f"/compact {instructions}"
        try:
            async with ClaudeSDKClient(options=options) as client:
                before = await client.get_context_usage()
                tokens_before = int(before.get("totalTokens", 0))
                await client.query(prompt)
                # Drain until ResultMessage to make sure compaction completes
                # before we re-sample.
                async for msg in client.receive_response():
                    if isinstance(msg, ResultMessage):
                        break
                after = await client.get_context_usage()
                tokens_after = int(after.get("totalTokens", 0))
        except Exception as e:  # noqa: BLE001 — surface as failure, not crash
            logger.exception("ClaudeSdkHarness.compact failed")
            return CompactResult(
                succeeded=False,
                message=f"Compact failed: {e}",
            )
        return CompactResult(
            succeeded=True,
            message=(
                f"Context compacted: {_fmt_k(tokens_before)} → {_fmt_k(tokens_after)}"
            ),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )

    # ---- Internals ----

    def _build_options(self, req: TurnRequest, options_cls):
        """Map TurnRequest → ClaudeAgentOptions.

        `extra_args` is the escape hatch for CLI flags the SDK doesn't
        promote to first-class fields (today: --plugin-dir).
        """
        extra: dict[str, str | None] = {}
        if req.plugin_dir:
            extra["plugin-dir"] = req.plugin_dir

        # The SDK wraps a string into a SystemPromptPreset when both a preset
        # and an append are needed. For our use case (append our own custom
        # text to the default claude_code preset), pass the dict form so the
        # SDK knows to extend the preset rather than replace it.
        system_prompt = (
            {"type": "preset", "preset": "claude_code", "append": req.system_prompt}
            if req.system_prompt
            else None
        )

        kwargs = {
            "cli_path": self._cli_path,
            "cwd": str(req.project_dir),
            "system_prompt": system_prompt,
            "max_turns": req.max_turns if req.max_turns is not None else self._max_turns_default,
            "permission_mode": "bypassPermissions",
            "extra_args": extra,
        }
        if req.allowed_tools:
            kwargs["allowed_tools"] = req.allowed_tools
        if req.disallowed_tools:
            kwargs["disallowed_tools"] = req.disallowed_tools
        if req.model:
            kwargs["model"] = req.model
        if req.effort:
            kwargs["effort"] = req.effort
        if req.resume_session_id:
            kwargs["resume"] = req.resume_session_id
        return options_cls(**kwargs)

    def _terminator_from_result(self, msg, text_chunks: list[str]):
        """Map a ResultMessage to either a TurnFinal or a TurnError."""
        # msg.result is the CLI's native final-answer field — only the last
        # assistant turn. text_chunks accumulates ALL turns (tool reasoning +
        # final), which is wrong for the response. Prefer msg.result.
        full_text = msg.result or "".join(text_chunks) or ""
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
        # Backstop string-match for rate limits that didn't surface as a typed
        # AssistantMessage.error (e.g. errors that come back inside the
        # ResultMessage.errors list instead).
        synthetic_event = {
            "type": "result",
            "error": " ".join(msg.errors) if msg.errors else "",
            "subtype": subtype,
        }
        if is_quota_error([synthetic_event], ""):
            return TurnError(
                kind="rate_limit",
                message="Rate limit detected in ResultMessage",
                retryable=False,
                metadata={"subtype": subtype, "errors": msg.errors},
            )
        if msg.is_error:
            return TurnError(
                kind="unknown",
                message=" ".join(msg.errors) if msg.errors else f"SDK reported error (subtype={subtype})",
                retryable=False,
                metadata={
                    "session_id": msg.session_id,
                    "subtype": subtype,
                    "errors": msg.errors,
                },
            )
        return TurnFinal(
            session_id=msg.session_id,
            num_turns=msg.num_turns,
            total_cost_usd=msg.total_cost_usd,
            raw_text=full_text or "(no parseable response)",
        )

    def _error_from_process_error(self, exc) -> TurnError:
        """Map a ProcessError to a TurnError, classifying by exit_code."""
        exit_code = getattr(exc, "exit_code", None)
        stderr = getattr(exc, "stderr", "") or ""
        if exit_code in (137, -9):
            return TurnError(
                kind="oom",
                message=f"CLI subprocess OOM-killed (rc={exit_code})",
                retryable=True,
                metadata={"exit_code": exit_code, "stderr": stderr[:200]},
            )
        if is_quota_error([], stderr):
            return TurnError(
                kind="rate_limit",
                message="Rate limit detected in CLI stderr",
                retryable=False,
                metadata={"exit_code": exit_code, "stderr": stderr[:200]},
            )
        if stderr and "No conversation found" in stderr:
            return TurnError(
                kind="corrupt_session",
                message="Stale or missing session id (from CLI stderr)",
                retryable=True,
                metadata={"exit_code": exit_code, "stderr": stderr[:200]},
            )
        return TurnError(
            kind="process_died",
            message=f"CLI subprocess failed (rc={exit_code}): {stderr[:200]}",
            retryable=True,
            metadata={"exit_code": exit_code, "stderr": stderr[:200]},
        )


def _fmt_k(n: int) -> str:
    """Render token counts as '12.3k' / '1.0M' for short messages."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _stringify_tool_output(content) -> str:
    """ToolResultBlock.content is str | list[dict] | None. Reduce to a string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # List of {"type": "text", "text": "..."} blocks per Anthropic API shape.
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content") or ""
                parts.append(str(t))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


# Type assertion: ClaudeSdkHarness conforms to the Harness protocol.
_protocol_check: Harness = ClaudeSdkHarness()  # noqa: F841
