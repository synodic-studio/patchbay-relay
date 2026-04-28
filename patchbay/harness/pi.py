"""PiHarness — wraps the `pi` CLI subprocess.

Pi (badlogicgames/pi) is a multi-model coding agent with a structured
JSON streaming protocol very similar to claude's. It uses any of a
dozen model providers (anthropic, openai, deepseek, openrouter, ...)
and stores per-cwd sessions in ~/.pi/agent/sessions.

Phase 5a of. Same shape as ClaudeCliHarness — sync Popen,
drain stdout/stderr, parse JSON-lines after the proc exits.

CLI surface mapping (see `pi --help`):

    --print, -p              non-interactive
    --mode json              one JSON event per line on stdout
    --session <id>           resume by partial UUID
    --no-session             don't persist session
    --model PROVIDER/ID      explicit provider+model
    --thinking <level>       off | minimal | low | medium | high | xhigh
    --append-system-prompt   appended to default system prompt
    --tools <csv>            allowlist
    --no-tools               disable all
    --no-extensions          (etc — opt-in via TurnRequest.extra)

Pi event types we care about:

    {"type":"session", "id":"019dc...", ...}     — first event, has session id
    {"type":"agent_start"} / {"type":"agent_end"}
    {"type":"turn_start"} / {"type":"turn_end"}
    {"type":"message_start", "message":{...}}    — assistant or user
    {"type":"message_update", "assistantMessageEvent":{
        "type":"text_delta",   "delta":"..."     — incremental text
        "type":"text_end",     "content":"..."
        "type":"toolcall_end", ... full call args
    }}
    {"type":"tool_execution_end", ...}            — observed tool result
    {"type":"agent_end", "messages":[...]}        — terminator

Errors are signaled by `stopReason: "error"` + `errorMessage` on the
terminal assistant message. Rate-limit detection matches the same
substrings the bridge uses for claude.
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

# Default location of the `pi` binary. Resolved at module load so tests
# can monkeypatch via the constructor without touching the filesystem.
PI_PATH_DEFAULT = shutil.which("pi") or "/opt/homebrew/bin/pi"


_CAPABILITIES = HarnessCapabilities(
    supports_resume=True,
    supports_tool_streaming=True,   # toolcall_end + tool_execution_end events
    supports_interrupt=False,        # SIGKILL on /kill
    supports_effort=False,           # pi exposes --thinking but it's per-model
    supports_mcp=False,              # pi has extensions, not MCP
)

_RATE_LIMIT_TOKENS = (
    "rate limit",
    "rate_limit",
    "rate-limited",
    "quota",
    "exceeded your current",
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


class PiHarness:
    """Run a turn via `pi -p --mode json`, yield TurnEvents."""

    name = "pi"
    capabilities = _CAPABILITIES

    def __init__(
        self,
        *,
        pi_path: str = PI_PATH_DEFAULT,
        max_timeout_seconds: float = MAX_TIMEOUT,
        on_progress: Callable[[], None] | None = None,
        proc_setter: Callable[[subprocess.Popen | None], None] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self._pi_path = pi_path
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
                message=f"Timed out after {self._max_timeout / 60:.0f} min",
                retryable=True,
                metadata={"duration": result.duration},
            )
            return

        stdout = result.stdout.strip()
        stderr = result.stderr or ""

        # No output: classify by exit code / stderr.
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

        events = list(_parse_pi_events(stdout))

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

        # Walk events: extract session id, text, tool use, tool results.
        session_id = _find_session_id(events)
        text = _extract_text(events)
        num_turns = sum(1 for e in events if e.get("type") == "turn_end") or None
        total_cost = _extract_total_cost(events)

        # Tool use / result events for activity logging.
        for ev in events:
            if ev.get("type") == "message_update":
                ame = ev.get("assistantMessageEvent") or {}
                if ame.get("type") == "toolcall_end":
                    yield ToolUse(
                        name=str(ame.get("name", "")),
                        input=ame.get("input", {}) or {},
                        id=ame.get("toolCallId") or ame.get("id"),
                    )
            elif ev.get("type") == "tool_execution_end":
                output = ev.get("output") or ev.get("result") or ""
                if isinstance(output, (dict, list)):
                    output = json.dumps(output)
                yield ToolResult(
                    tool_use_id=ev.get("toolCallId") or ev.get("id"),
                    output=str(output)[:4000],
                    is_error=bool(ev.get("isError")),
                )

        # Error detection: stopReason=='error' on any assistant message.
        err_msg = _find_assistant_error(events)
        if err_msg is not None:
            kind = (
                "rate_limit"
                if any(tok in err_msg.lower() for tok in _RATE_LIMIT_TOKENS)
                else "unknown"
            )
            yield TurnError(
                kind=kind,
                message=err_msg[:500],
                retryable=(kind == "rate_limit"),
                metadata={
                    "session_id": session_id,
                    "duration": result.duration,
                },
            )
            return

        if not text:
            placeholder = (
                f"(Pi completed {num_turns or '?'} turns but produced no text response. "
                "Tool work may have happened — check the project files.)"
            )
            yield TextDelta(text=placeholder, final=True)
            yield TurnFinal(
                session_id=session_id,
                num_turns=num_turns,
                total_cost_usd=total_cost,
                raw_text=placeholder,
            )
            return

        yield TextDelta(text=text, final=True)
        yield TurnFinal(
            session_id=session_id,
            num_turns=num_turns,
            total_cost_usd=total_cost,
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
            self._pi_path,
            "-p",
            "--mode",
            "json",
        ]
        if req.resume_session_id:
            cmd.extend(["--session", req.resume_session_id])
        if req.system_prompt:
            cmd.extend(["--append-system-prompt", req.system_prompt])
        if req.model:
            cmd.extend(["--model", req.model])
        if req.allowed_tools:
            cmd.extend(["--tools", ",".join(req.allowed_tools)])
        if req.disallowed_tools and not req.allowed_tools:
            # Pi has no native denylist; closest we can do is --no-tools
            # if the caller is trying to disable everything.
            if "*" in req.disallowed_tools:
                cmd.append("--no-tools")
        # Caller-supplied extras: thinking level, no-extensions flags, etc.
        extra = req.extra or {}
        if isinstance(extra.get("thinking"), str):
            cmd.extend(["--thinking", extra["thinking"]])
        if extra.get("no_extensions"):
            cmd.append("--no-extensions")
        if extra.get("no_skills"):
            cmd.append("--no-skills")
        if extra.get("no_context_files"):
            cmd.append("--no-context-files")
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
                logger.exception("pi proc_setter callback raised")

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
                    logger.exception("pi proc_setter callback raised")

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
                            logger.exception("pi on_progress callback raised")
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
        """Pre-event classification when stdout is empty."""
        # Pi-equivalent of "stale resume": if stderr mentions session not found
        # and we passed --session, the bridge can retry without it.
        if (
            req.resume_session_id is not None
            and stderr
            and ("session not found" in stderr.lower() or "no such session" in stderr.lower())
        ):
            return TurnError(
                kind="corrupt_session",
                message="Pi session not found; retry without resume",
                retryable=True,
                metadata={
                    "stale_session_id": req.resume_session_id,
                    "exit_code": result.returncode,
                },
            )

        # OOM-shaped exit.
        if result.returncode in (137, -9):
            return TurnError(
                kind="oom",
                message=f"Pi subprocess OOM-killed (rc={result.returncode})",
                retryable=True,
                metadata={"exit_code": result.returncode, "stderr": stderr[:200]},
            )

        # Rate limit visible in stderr only.
        if stderr and any(tok in stderr.lower() for tok in _RATE_LIMIT_TOKENS):
            return TurnError(
                kind="rate_limit",
                message="Rate limit detected in stderr",
                retryable=False,
                metadata={"stderr": stderr[:200], "duration": result.duration},
            )

        return None


# ---- Module-level helpers (kept module-level for testability) ----


def _parse_pi_events(stdout: str):
    """Yield parsed JSON event dicts from pi's `--mode json` stdout.

    Lines that fail to parse are skipped silently; pi mostly emits one
    event per line but we tolerate occasional non-JSON lines.
    """
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _find_session_id(events: list[dict]) -> str | None:
    for ev in events:
        if ev.get("type") == "session":
            sid = ev.get("id")
            if isinstance(sid, str):
                return sid
    return None


def _extract_text(events: list[dict]) -> str:
    """Aggregate assistant text from message_update text_delta events.

    We prefer the assembled text on `text_end` events when present (it's
    the canonical content for the block); we fall back to summing
    `text_delta`s if no text_end was seen.
    """
    texts: list[str] = []
    pending: dict[int, list[str]] = {}
    for ev in events:
        if ev.get("type") != "message_update":
            continue
        ame = ev.get("assistantMessageEvent") or {}
        kind = ame.get("type")
        idx = ame.get("contentIndex", 0)
        if kind == "text_delta":
            delta = ame.get("delta", "")
            if isinstance(delta, str) and delta:
                pending.setdefault(idx, []).append(delta)
        elif kind == "text_end":
            content = ame.get("content")
            if isinstance(content, str) and content:
                texts.append(content)
                pending.pop(idx, None)
            elif idx in pending:
                texts.append("".join(pending.pop(idx)))
    # Any pending blocks that never saw text_end: flush the deltas.
    for chunk in pending.values():
        texts.append("".join(chunk))
    return "\n".join(t for t in texts if t).strip()


def _extract_total_cost(events: list[dict]) -> float | None:
    """Sum cost.total across assistant turn_end events (one per turn).

    We use turn_end (not message_end) to avoid double-counting: the same
    cost appears on the turn's last message_end and the turn_end that
    follows it.
    """
    total = 0.0
    seen = False
    for ev in events:
        if ev.get("type") != "turn_end":
            continue
        msg = ev.get("message") or {}
        usage = msg.get("usage") or {}
        cost = usage.get("cost") or {}
        c = cost.get("total")
        if isinstance(c, (int, float)):
            total += float(c)
            seen = True
    return total if seen else None


def _find_assistant_error(events: list[dict]) -> str | None:
    """Return the first assistant errorMessage we find, else None."""
    for ev in events:
        if ev.get("type") not in ("message_end", "turn_end"):
            continue
        msg = ev.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        if msg.get("stopReason") == "error":
            err = msg.get("errorMessage")
            if isinstance(err, str) and err:
                return err
    return None


# Type-check at module load (mirrors claude_cli convention).
_protocol_check: Harness = PiHarness()  # noqa: F841


__all__ = ["PiHarness", "PI_PATH_DEFAULT"]
