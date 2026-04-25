"""OpenCodeHarness — wraps `opencode run --format json` subprocess.

OpenCode (sst/opencode) is a TUI-first coding agent with a clean
JSON event protocol when invoked via `opencode run --format json`.
Sessions are first-class (`ses_*` ids) and resumable via --session.

Phase 5c of CTB-cyz.

Event types we consume:
  step_start    — start of a turn step
  text          — assistant text (full text per event, not incremental)
  tool_use      — tool call w/ embedded result; part.state.status==completed
                  carries input + output in one event
  step_finish   — end of step; part.tokens has totals + cost
  error         — failure with part.error.data.message

Session resume: opencode emits `sessionID` on every event; we pull it
from the first event and feed it back via `--session <id>` next turn.

Defaults:
- model: STARGATE_OPENCODE_MODEL env or "openrouter/deepseek/deepseek-chat-v3.1"
- subprocess: --format json --pure --dangerously-skip-permissions

Capabilities:
- supports_resume=True
- supports_tool_streaming=True
- supports_interrupt=False (SIGKILL)
- supports_effort=True (via --variant high/max/minimal)
- supports_mcp=True
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from ..config import MAX_TIMEOUT, logger
from .base import (
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


OPENCODE_PATH_DEFAULT = shutil.which("opencode") or "/opt/homebrew/bin/opencode"

DEFAULT_OPENCODE_MODEL = os.environ.get(
    "STARGATE_OPENCODE_MODEL", "openrouter/deepseek/deepseek-chat-v3.1"
)


_CAPABILITIES = HarnessCapabilities(
    supports_resume=True,
    supports_tool_streaming=True,
    supports_interrupt=False,
    supports_effort=True,
    supports_mcp=True,
)


_RATE_LIMIT_TOKENS = (
    "rate limit",
    "rate_limit",
    "ratelimiterror",
    "quota",
    " 429 ",
    "too many requests",
    "insufficient_quota",
)


@dataclass
class _RunResult:
    stdout: str
    stderr: str
    returncode: int
    duration: float
    timed_out: bool = False


class OpenCodeHarness:
    """Run a turn via `opencode run --format json`, yield TurnEvents."""

    name = "opencode"
    capabilities = _CAPABILITIES

    def __init__(
        self,
        *,
        opencode_path: str = OPENCODE_PATH_DEFAULT,
        default_model: str = DEFAULT_OPENCODE_MODEL,
        max_timeout_seconds: float = MAX_TIMEOUT,
        on_progress: Callable[[], None] | None = None,
        proc_setter: Callable[[subprocess.Popen | None], None] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self._opencode_path = opencode_path
        self._default_model = default_model
        self._max_timeout = max_timeout_seconds
        self._on_progress = on_progress
        self._proc_setter = proc_setter
        self._env_overrides = env_overrides or {}
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()

    # ---- Public API ----

    async def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]:
        cmd = self._build_cmd(req)
        loop = asyncio.get_running_loop()
        result: _RunResult = await loop.run_in_executor(
            None, lambda: self._run_subprocess(cmd, req.project_dir)
        )

        if result.timed_out:
            yield TurnError(
                kind="timeout",
                message=f"Opencode timed out after {self._max_timeout / 60:.0f} min",
                retryable=True,
                metadata={"duration": result.duration},
            )
            return

        stdout = result.stdout.strip()
        stderr = result.stderr or ""

        if not stdout:
            err = self._classify_no_output(result, stderr, req)
            if err is not None:
                yield err
                return
            yield TurnError(
                kind="unknown",
                message=(
                    f"(no output. stderr: {stderr[:500]})" if stderr else "(no output)"
                ),
                retryable=False,
                metadata={"exit_code": result.returncode, "stderr": stderr[:500]},
            )
            return

        events = list(_parse_opencode_events(stdout))

        if not events:
            fallback = stdout.strip() or "(no parseable response)"
            yield TextDelta(text=fallback, final=True)
            yield TurnFinal(
                session_id=None,
                num_turns=None,
                total_cost_usd=None,
                raw_text=fallback,
            )
            return

        session_id = _find_session_id(events)
        text_chunks: list[str] = []
        total_cost = 0.0
        cost_seen = False
        num_steps = 0

        for ev in events:
            etype = ev.get("type")
            part = ev.get("part") or {}
            if etype == "text":
                t = part.get("text")
                if isinstance(t, str) and t:
                    text_chunks.append(t)
            elif etype == "tool_use":
                state = part.get("state") or {}
                tool_name = part.get("tool", "")
                call_id = part.get("callID")
                yield ToolUse(
                    name=str(tool_name),
                    input=state.get("input", {}) or {},
                    id=call_id,
                )
                if state.get("status") == "completed":
                    out = state.get("output") or ""
                    if isinstance(out, (dict, list)):
                        out = json.dumps(out)
                    yield ToolResult(
                        tool_use_id=call_id,
                        output=str(out)[:4000],
                        is_error=bool(state.get("error")),
                    )
            elif etype == "step_finish":
                num_steps += 1
                cost = part.get("cost")
                if isinstance(cost, (int, float)):
                    total_cost += float(cost)
                    cost_seen = True
                tokens = part.get("tokens") or {}
                cost2 = tokens.get("cost") if isinstance(tokens, dict) else None
                if isinstance(cost2, (int, float)):
                    total_cost += float(cost2)
                    cost_seen = True

        # Error event terminates regardless of any preceding text.
        err_event = next((e for e in events if e.get("type") == "error"), None)
        if err_event is not None:
            err_data = (err_event.get("error") or {}).get("data") or {}
            err_msg = err_data.get("message") or err_data.get("error") or "opencode error"
            kind = (
                "rate_limit"
                if any(tok in err_msg.lower() for tok in _RATE_LIMIT_TOKENS)
                else "unknown"
            )
            yield TurnError(
                kind=kind,
                message=str(err_msg)[:500],
                retryable=(kind == "rate_limit"),
                metadata={
                    "session_id": session_id,
                    "duration": result.duration,
                },
            )
            return

        text = "\n".join(t for t in text_chunks if t).strip()

        if not text:
            placeholder = (
                f"(Opencode finished {num_steps or '?'} step(s) with no text response. "
                "Tool work may have happened — check the project files.)"
            )
            yield TextDelta(text=placeholder, final=True)
            yield TurnFinal(
                session_id=session_id,
                num_turns=num_steps or None,
                total_cost_usd=total_cost if cost_seen else None,
                raw_text=placeholder,
            )
            return

        yield TextDelta(text=text, final=True)
        yield TurnFinal(
            session_id=session_id,
            num_turns=num_steps or None,
            total_cost_usd=total_cost if cost_seen else None,
            raw_text=text,
        )

    async def cancel(self) -> None:
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
        cmd: list[str] = [
            self._opencode_path,
            "run",
            "--format",
            "json",
            "--pure",
            "--dangerously-skip-permissions",
        ]
        if req.resume_session_id:
            cmd.extend(["--session", req.resume_session_id])
        model = req.model or self._default_model
        cmd.extend(["--model", model])
        if req.effort:
            # opencode --variant accepts provider-specific reasoning levels
            # (high, max, minimal). Pass through whatever the caller gave.
            cmd.extend(["--variant", req.effort])
        cmd.append(req.prompt)
        return cmd

    def _run_subprocess(self, cmd: list[str], cwd) -> _RunResult:
        invoke_start = time.time()
        env = os.environ.copy()
        env.update(self._env_overrides)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd),
            env=env,
        )
        with self._proc_lock:
            self._proc = proc
        if self._proc_setter is not None:
            try:
                self._proc_setter(proc)
            except Exception:  # noqa: BLE001
                logger.exception("opencode proc_setter callback raised")

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
                    logger.exception("opencode proc_setter callback raised")

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
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []

        def _drain(stream, buf, mark_event: bool) -> None:
            try:
                for line in iter(stream.readline, ""):
                    buf.append(line)
                    if mark_event and self._on_progress is not None:
                        try:
                            self._on_progress()
                        except Exception:  # noqa: BLE001
                            logger.exception("opencode on_progress callback raised")
            except (OSError, ValueError):
                pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        t_out = threading.Thread(
            target=_drain, args=(proc.stdout, stdout_buf, True), daemon=True
        )
        t_err = threading.Thread(
            target=_drain, args=(proc.stderr, stderr_buf, False), daemon=True
        )
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
        if (
            req.resume_session_id is not None
            and stderr
            and ("session not found" in stderr.lower() or "no such session" in stderr.lower())
        ):
            return TurnError(
                kind="corrupt_session",
                message="Opencode session not found; retry without resume",
                retryable=True,
                metadata={
                    "stale_session_id": req.resume_session_id,
                    "exit_code": result.returncode,
                },
            )
        if result.returncode in (137, -9):
            return TurnError(
                kind="oom",
                message=f"Opencode subprocess OOM-killed (rc={result.returncode})",
                retryable=True,
                metadata={"exit_code": result.returncode, "stderr": stderr[:200]},
            )
        if stderr and any(tok in stderr.lower() for tok in _RATE_LIMIT_TOKENS):
            return TurnError(
                kind="rate_limit",
                message="Rate limit detected in opencode stderr",
                retryable=False,
                metadata={"stderr": stderr[:200]},
            )
        return None


# ---- Helpers ----


def _parse_opencode_events(stdout: str):
    """Yield JSON event dicts. Skips non-JSON noise (banner lines, etc.)."""
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _find_session_id(events: list[dict]) -> str | None:
    for ev in events:
        sid = ev.get("sessionID")
        if isinstance(sid, str) and sid:
            return sid
    return None


_protocol_check: Harness = OpenCodeHarness()  # noqa: F841

__all__ = [
    "OpenCodeHarness",
    "OPENCODE_PATH_DEFAULT",
    "DEFAULT_OPENCODE_MODEL",
]
