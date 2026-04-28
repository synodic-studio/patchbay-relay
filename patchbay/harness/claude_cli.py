"""ClaudeCliHarness — wraps `claude -p` subprocess.

This is the phase-1 harness: it owns the Popen, stdout/stderr drain, JSON
parsing, and error classification that today live inline in
`bridge.run_claude`. The bridge will switch to call this harness in
phase 1b; until then, both code paths exist.

Design constraints:
- No import of `bridge` (would create a cycle).
- Stall detection lives in the bridge — this harness exposes an
  `on_progress` callback the bridge wires to its `SessionState.last_event_at`.
- All knobs come in via `TurnRequest`. Per-call overrides (model, effort,
  resume) are TurnRequest fields; per-process config (CLAUDE_PATH,
  MAX_TIMEOUT) is constructor args with sensible defaults from
  `patchbay.config`.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from ..config import CLAUDE_PATH, MAX_TIMEOUT, MAX_TURNS, logger
from ..parser import _extract_text_from_events, _parse_events, is_empty_success_response
from ..quota import is_quota_error
from .base import (
    Harness,
    HarnessCapabilities,
    TextDelta,
    ToolUse,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)

_CAPABILITIES = HarnessCapabilities(
    supports_resume=True,
    supports_tool_streaming=False,  # CLI buffers; tool events arrive post-hoc
    supports_interrupt=False,        # we SIGKILL on /kill
    supports_effort=True,
    supports_mcp=True,
)


@dataclass
class _RunResult:
    """Output of one synchronous Popen invocation. Internal to the harness."""

    stdout: str
    stderr: str
    returncode: int
    duration: float
    timed_out: bool = False


class ClaudeCliHarness:
    """Run a turn via `claude -p` subprocess, yield TurnEvents."""

    name = "cc-cli"
    capabilities = _CAPABILITIES

    def __init__(
        self,
        *,
        claude_path: str = CLAUDE_PATH,
        max_timeout_seconds: float = MAX_TIMEOUT,
        max_turns_default: int = MAX_TURNS,
        on_progress: Callable[[], None] | None = None,
        proc_setter: Callable[[subprocess.Popen | None], None] | None = None,
    ) -> None:
        self._claude_path = claude_path
        self._max_timeout = max_timeout_seconds
        self._max_turns_default = max_turns_default
        self._on_progress = on_progress
        # External observer that wants to mirror the active proc handle
        # (used by the bridge to populate SessionState.proc so /kill,
        # the stall detector, and graceful shutdown can reach it).
        self._proc_setter = proc_setter
        # Track the active subprocess so cancel() can kill it.
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()

    # ---- Public API ----

    async def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]:
        """See Harness.run_turn. Always yields exactly one TurnFinal or TurnError last."""
        cmd = self._build_cmd(req)
        loop = asyncio.get_running_loop()
        result: _RunResult = await loop.run_in_executor(
            None, lambda: self._run_subprocess(cmd, req.project_dir)
        )

        # Timeout: classify and exit early.
        if result.timed_out:
            yield TurnError(
                kind="timeout",
                message=f"Timed out after {self._max_timeout / 60:.0f} min",
                retryable=True,
                metadata={"duration": result.duration},
            )
            return

        stdout = result.stdout.strip()
        stderr = result.stderr or ""

        # No output at all: classify by stderr / exit code.
        if not stdout:
            err = self._classify_no_output(result, stderr, req)
            if err is not None:
                yield err
                return
            yield TurnError(
                kind="unknown",
                message=f"(no output. stderr: {stderr[:500]})" if stderr else "(no output)",
                retryable=False,
                metadata={"exit_code": result.returncode, "stderr": stderr[:500]},
            )
            return

        # Have stdout. Parse and emit.
        events = _parse_events(stdout)

        # Legacy fallback: if stdout didn't parse to any events, surface the
        # raw text verbatim so the user/operator can see what claude said.
        # parser.parse_claude_response did the same with `stdout.strip() or
        # "(no parseable response)"`.
        if not events:
            fallback_text = stdout.strip() or "(no parseable response)"
            yield TextDelta(text=fallback_text, final=True)
            if result.returncode != 0 and stderr:
                yield TurnError(
                    kind="unknown",
                    message=f"(Claude exited with error: {stderr[:500]})",
                    retryable=False,
                    metadata={"exit_code": result.returncode, "stderr": stderr[:500]},
                )
                return
            yield TurnFinal(
                session_id=None,
                num_turns=None,
                total_cost_usd=None,
                raw_text=fallback_text,
            )
            return

        # Quota detection runs over parsed events + stderr.
        if is_quota_error(events, stderr):
            yield TurnError(
                kind="rate_limit",
                message="Quota or rate limit hit",
                retryable=False,  # bridge does Forge handoff, not retry
                metadata={"duration": result.duration},
            )
            return

        # Surface tool calls so the bridge's activity log can record them.
        for event in events:
            if event.get("type") != "assistant":
                continue
            msg = event.get("message", {})
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", []) or []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    yield ToolUse(
                        name=str(block.get("name", "")),
                        input=block.get("input", {}) or {},
                        id=block.get("id"),
                    )

        # Result event: extract session_id, max_turns subtype, costs.
        result_event = next(
            (e for e in reversed(events) if e.get("type") == "result"), None
        )
        session_id = result_event.get("session_id") if result_event else None
        num_turns = result_event.get("num_turns") if result_event else None
        cost = result_event.get("total_cost_usd") if result_event else None

        # Extract the final assistant text.
        text = _extract_text_from_events(events) or ""
        subtype = (
            (result_event.get("subtype") or result_event.get("result_subtype"))
            if result_event
            else None
        )

        if subtype in ("max_turns", "error_max_turns"):
            notice = f"\n\n[Reached {self._max_turns_default}-turn limit. Session preserved — reply to continue.]"
            final_text = (
                (text + notice)
                if text
                else f"(Session used {num_turns or '?'} turns / ${cost or '?'} but produced no text response. "
                "Work may have been done via tools — check the agent's files. Reply to continue.)"
            )
            yield TextDelta(text=final_text, final=True)
            yield TurnError(
                kind="max_turns",
                message=final_text,
                retryable=False,
                metadata={
                    "session_id": session_id,
                    "num_turns": num_turns,
                    "total_cost_usd": cost,
                },
            )
            return

        # Empty-success: bridge handles summary-retry today; we surface the
        # placeholder text and let the caller decide. Marked via metadata so
        # phase 1b's bridge wrapper can detect and retry.
        if not text and result_event and subtype == "success":
            placeholder = (
                f"(Completed {num_turns or '?'} turns of work but didn't produce a text response. "
                "Check agent files for results.)"
            )
            yield TextDelta(text=placeholder, final=True)
            yield TurnFinal(
                session_id=session_id,
                num_turns=num_turns,
                total_cost_usd=cost,
                raw_text=placeholder,
            )
            return

        # Result event has an error field set: surface it.
        if not text and result_event and result_event.get("error"):
            err_text = f"(Claude error: {result_event['error']})"
            yield TextDelta(text=err_text, final=True)
            yield TurnError(
                kind="unknown",
                message=err_text,
                retryable=False,
                metadata={"session_id": session_id, "error": result_event.get("error")},
            )
            return

        # Normal success path.
        if text:
            yield TextDelta(text=text, final=True)
            yield TurnFinal(
                session_id=session_id,
                num_turns=num_turns,
                total_cost_usd=cost,
                raw_text=text,
            )
            return

        # No extractable text and no classified result_event subtype: this
        # is "(no parseable response)" territory. If the proc also exited
        # nonzero with stderr, surface that as a classified error so the
        # bridge can render the stderr to the user.
        if result.returncode != 0 and stderr:
            yield TurnError(
                kind="unknown",
                message=f"(Claude exited with error: {stderr[:500]})",
                retryable=False,
                metadata={"exit_code": result.returncode, "stderr": stderr[:500]},
            )
            return

        yield TextDelta(text="(no parseable response)", final=True)
        yield TurnFinal(
            session_id=session_id,
            num_turns=num_turns,
            total_cost_usd=cost,
            raw_text="(no parseable response)",
        )

    async def cancel(self) -> None:
        """Kill the active subprocess, if any. Idempotent."""
        with self._proc_lock:
            proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: proc.wait(timeout=2)
            )
        except subprocess.TimeoutExpired:
            pass

    # ---- Internals ----

    def _build_cmd(self, req: TurnRequest) -> list[str]:
        """Construct the `claude -p ...` argv. Mirrors bridge.run_claude today."""
        max_turns = req.max_turns if req.max_turns is not None else self._max_turns_default
        # stream-json (not json) so the bridge's stall detector gets per-event
        # stdout cadence. See bridge.run_claude for the same change with
        # context.
        cmd: list[str] = [
            self._claude_path,
            "-p",
            req.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--max-turns",
            str(max_turns),
        ]
        if req.disallowed_tools:
            cmd.extend(["--disallowed-tools", ",".join(req.disallowed_tools)])
        if req.allowed_tools:
            cmd.extend(["--allowed-tools", ",".join(req.allowed_tools)])
        if req.plugin_dir:
            cmd.extend(["--plugin-dir", req.plugin_dir])
        if req.system_prompt:
            cmd.extend(["--append-system-prompt", req.system_prompt])
        if req.model:
            cmd.extend(["--model", req.model])
        if req.effort:
            cmd.extend(["--effort", req.effort])
        if req.resume_session_id:
            cmd.extend(["--resume", req.resume_session_id])
        return cmd

    def _run_subprocess(self, cmd: list[str], cwd) -> _RunResult:
        """Sync Popen + stream drain. Runs on a worker thread."""
        invoke_start = time.time()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd),
        )
        with self._proc_lock:
            self._proc = proc
        if self._proc_setter is not None:
            try:
                self._proc_setter(proc)
            except Exception:  # noqa: BLE001 — never let a callback bring us down
                logger.exception("proc_setter callback raised")

        try:
            stdout, stderr = self._drain_streams(proc, self._max_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            return _RunResult(
                stdout="",
                stderr="",
                returncode=proc.returncode or -1,
                duration=time.time() - invoke_start,
                timed_out=True,
            )
        finally:
            with self._proc_lock:
                self._proc = None
            if self._proc_setter is not None:
                try:
                    self._proc_setter(None)
                except Exception:  # noqa: BLE001
                    logger.exception("proc_setter callback raised")

        return _RunResult(
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
            duration=time.time() - invoke_start,
            timed_out=False,
        )

    def _drain_streams(
        self, proc: subprocess.Popen, timeout: float
    ) -> tuple[str, str]:
        """Same shape as bridge._read_proc_streaming, minus SessionState.

        Reader threads drain stdout/stderr while the main thread waits for
        the process to exit. Calls `on_progress` (if set) on every stdout
        line so the bridge can update its stall timer.
        """
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []

        def _drain(stream, buf, mark_event: bool) -> None:
            try:
                for line in iter(stream.readline, ""):
                    buf.append(line)
                    if mark_event and self._on_progress is not None:
                        try:
                            self._on_progress()
                        except Exception:  # noqa: BLE001 — never let a callback bring us down
                            logger.exception("on_progress callback raised")
            except (OSError, ValueError):
                pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_buf, True), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf, False), daemon=True)
        t_out.start()
        t_err.start()

        proc.wait(timeout=timeout)
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        return "".join(stdout_buf), "".join(stderr_buf)

    def _classify_no_output(
        self,
        result: _RunResult,
        stderr: str,
        req: TurnRequest,
    ) -> TurnError | None:
        """Classify a failure when stdout is empty.

        Returns a TurnError if classifiable, else None (caller emits a
        generic fallback). Mirrors the existing run_claude branch order:
        stale session > OOM > quota > unknown.
        """
        # Stale resume: caller can retry without resume_session_id.
        if (
            stderr
            and "No conversation found" in stderr
            and req.resume_session_id is not None
        ):
            return TurnError(
                kind="corrupt_session",
                message="Stale or missing session id; retry without resume",
                retryable=True,
                metadata={
                    "stale_session_id": req.resume_session_id,
                    "exit_code": result.returncode,
                },
            )

        # OOM-shaped exit: bridge dispatches self_heal and retries with
        # tighter turn budget + trimmed prompt.
        if result.returncode in (137, -9):
            return TurnError(
                kind="oom",
                message=f"Subprocess OOM-killed (rc={result.returncode})",
                retryable=True,
                metadata={"exit_code": result.returncode, "stderr": stderr[:200]},
            )

        # Quota / rate-limit can show up in stderr only (no events to parse).
        if is_quota_error([], stderr):
            return TurnError(
                kind="rate_limit",
                message="Quota / rate limit detected in stderr",
                retryable=False,
                metadata={"stderr": stderr[:200], "duration": result.duration},
            )

        return None


# Re-export for type-narrowing tests that want to verify protocol conformance.
_protocol_check: Harness = ClaudeCliHarness()  # noqa: F841 — type assertion


# Small helper to detect empty-success placeholders in caller code (phase 1b).
__all__ = [
    "ClaudeCliHarness",
    "is_empty_success_response",  # re-exported for caller convenience
]
