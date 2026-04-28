"""AiderHarness — wraps `aider --message` subprocess.

Aider (Aider-AI/aider) is a model-agnostic pair-programming CLI that
edits files in cwd via SEARCH/REPLACE blocks and integrates with git.
It has no structured output mode — stdout is a header (5 lines), the
assistant response, and a footer ("Tokens: ... Cost: ..."). We
strip header/footer and surface the body verbatim.

Resume is via `--restore-chat-history --chat-history-file <path>`.
We own the path: patchbay/aider-history/<sanitized-session-key>.md.
The "session_id" the harness returns to the bridge IS that path; the
bridge passes it back as `resume_session_id` on the next turn.

Phase 5b of.

Defaults:
- model: PATCHBAY_AIDER_MODEL env (or legacy STARGATE_AIDER_MODEL) or
         "openrouter/deepseek/deepseek-chat"
- subprocess: --no-pretty --no-stream --yes-always --no-fancy-input
              --no-check-update --no-show-release-notes --analytics-disable
- git off by default (--no-git) — patchbay manages its own commits

Capabilities:
- supports_resume=True (chat-history file)
- supports_tool_streaming=False (unstructured stdout)
- supports_interrupt=False (SIGKILL)
- supports_effort=False (aider has --reasoning-effort but we don't map it yet)
- supports_mcp=False
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import AIDER_HISTORY_DIR, MAX_TIMEOUT, _env_with_legacy, logger
from .base import (
    Harness,
    HarnessCapabilities,
    TextDelta,
    TurnError,
    TurnEvent,
    TurnFinal,
    TurnRequest,
)

AIDER_PATH_DEFAULT = (
    shutil.which("aider")
    or os.path.expanduser("~/.local/bin/aider")
)

DEFAULT_AIDER_MODEL = _env_with_legacy(
    "PATCHBAY_AIDER_MODEL", "STARGATE_AIDER_MODEL",
    "openrouter/deepseek/deepseek-chat",
)


_CAPABILITIES = HarnessCapabilities(
    supports_resume=True,
    supports_tool_streaming=False,
    supports_interrupt=False,
    supports_effort=False,
    supports_mcp=False,
)


_RATE_LIMIT_TOKENS = (
    "rate limit",
    "ratelimiterror",
    "rate_limit",
    "quota",
    " 429 ",
    "too many requests",
    "insufficient_quota",
    "exceeded your current",
)


# Lines that appear in aider's startup banner; once we see a blank line
# *after* one of these, the body has begun.
_HEADER_PREFIXES = (
    "Aider v",
    "Model:",
    "Git repo:",
    "Repo-map:",
    "Restored previous conversation history.",
    "Analytics ",
    "Update available",
    "Warning:",
    "Note:",
)


_FOOTER_PREFIXES = (
    "Tokens:",
)


_SESSION_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class _RunResult:
    stdout: str
    stderr: str
    returncode: int
    duration: float
    timed_out: bool = False


class AiderHarness:
    """Run a turn via `aider --message`, return assistant response as TurnEvent."""

    name = "aider"
    capabilities = _CAPABILITIES

    def __init__(
        self,
        *,
        aider_path: str = AIDER_PATH_DEFAULT,
        history_dir: Path = AIDER_HISTORY_DIR,
        default_model: str = DEFAULT_AIDER_MODEL,
        max_timeout_seconds: float = MAX_TIMEOUT,
        on_progress: Callable[[], None] | None = None,
        proc_setter: Callable[[subprocess.Popen | None], None] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self._aider_path = aider_path
        self._history_dir = Path(history_dir)
        self._history_dir.mkdir(parents=True, exist_ok=True)
        self._default_model = default_model
        self._max_timeout = max_timeout_seconds
        self._on_progress = on_progress
        self._proc_setter = proc_setter
        self._env_overrides = env_overrides or {}
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()

    # ---- Public API ----

    async def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]:
        history_path = self._resolve_history_path(req)
        cmd = self._build_cmd(req, history_path)
        loop = asyncio.get_running_loop()
        result: _RunResult = await loop.run_in_executor(
            None, lambda: self._run_subprocess(cmd, req.project_dir)
        )

        if result.timed_out:
            yield TurnError(
                kind="timeout",
                message=f"Aider timed out after {self._max_timeout / 60:.0f} min",
                retryable=True,
                metadata={"duration": result.duration},
            )
            return

        stdout = result.stdout
        stderr = result.stderr or ""
        body = _strip_aider_chrome(stdout)
        cost = _parse_cost(stdout)
        combined_lower = (stdout + "\n" + stderr).lower()

        # Hard failure modes first.
        if not body and not stdout.strip():
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

        # Rate limit detection from stdout/stderr text.
        if any(tok in combined_lower for tok in _RATE_LIMIT_TOKENS):
            yield TurnError(
                kind="rate_limit",
                message="Aider hit a provider rate limit",
                retryable=False,
                metadata={
                    "session_id": str(history_path),
                    "stderr": stderr[:200],
                    "duration": result.duration,
                },
            )
            return

        # Nonzero exit is a hard failure unless we still got readable text.
        if result.returncode != 0 and not body:
            yield TurnError(
                kind="unknown",
                message=f"(Aider exited {result.returncode}: {stderr[:300] or 'no stderr'})",
                retryable=False,
                metadata={
                    "exit_code": result.returncode,
                    "stderr": stderr[:500],
                    "session_id": str(history_path),
                },
            )
            return

        text = body or "(Aider produced no readable response — check the chat history file.)"
        yield TextDelta(text=text, final=True)
        yield TurnFinal(
            session_id=str(history_path),
            num_turns=1,
            total_cost_usd=cost,
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

    def _resolve_history_path(self, req: TurnRequest) -> Path:
        """Use the resume id verbatim if it points inside our history dir,
        otherwise derive a stable path from the session key."""
        if req.resume_session_id:
            candidate = Path(req.resume_session_id)
            try:
                candidate.relative_to(self._history_dir)
                return candidate
            except ValueError:
                pass  # not in our dir — derive a fresh path
        sanitized = _SESSION_KEY_RE.sub("_", req.session_key)[:80] or "default"
        return self._history_dir / f"{sanitized}.md"

    def _build_cmd(self, req: TurnRequest, history_path: Path) -> list[str]:
        cmd: list[str] = [
            self._aider_path,
            "--no-pretty",
            "--no-stream",
            "--yes-always",
            "--no-fancy-input",
            "--no-check-update",
            "--no-show-release-notes",
            "--analytics-disable",
            "--no-git",
            "--no-show-model-warnings",
            "--chat-history-file",
            str(history_path),
            "--llm-history-file",
            str(history_path.with_suffix(".llm.log")),
        ]
        if req.resume_session_id and history_path.exists():
            cmd.append("--restore-chat-history")
        else:
            cmd.append("--no-restore-chat-history")

        model = req.model or self._default_model
        cmd.extend(["--model", model])

        if req.system_prompt:
            cmd.extend(["--read", _system_prompt_file(req.system_prompt, history_path)])

        cmd.extend(["--message", req.prompt])
        return cmd

    def _run_subprocess(self, cmd: list[str], cwd) -> _RunResult:
        invoke_start = time.time()
        env = os.environ.copy()
        env.update(self._env_overrides)
        # Aider is chatty about config files; suppress unless caller turned
        # it on explicitly.
        env.setdefault("AIDER_NO_BROWSER", "1")
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
                logger.exception("aider proc_setter callback raised")

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
                    logger.exception("aider proc_setter callback raised")

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
                            logger.exception("aider on_progress callback raised")
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
        if result.returncode in (137, -9):
            return TurnError(
                kind="oom",
                message=f"Aider subprocess OOM-killed (rc={result.returncode})",
                retryable=True,
                metadata={"exit_code": result.returncode, "stderr": stderr[:200]},
            )
        if stderr and any(tok in stderr.lower() for tok in _RATE_LIMIT_TOKENS):
            return TurnError(
                kind="rate_limit",
                message="Rate limit detected in aider stderr",
                retryable=False,
                metadata={"stderr": stderr[:200]},
            )
        return None


# ---- Module helpers ----


def _system_prompt_file(system_prompt: str, history_path: Path) -> str:
    """Aider has no --append-system-prompt; we drop the prompt to a sibling
    file and pass it as `--read <file>`. The file is rewritten each call."""
    sp_path = history_path.with_suffix(".system.md")
    sp_path.write_text(system_prompt)
    return str(sp_path)


def _strip_aider_chrome(stdout: str) -> str:
    """Remove the startup banner and the trailing 'Tokens:' line.

    Aider's banner is one or more header lines (Analytics, Aider vX,
    Model, Git repo, Repo-map, Restored ...), interspersed with blank
    lines, then a blank, then the body. We walk forward until we find
    a non-header non-blank line; everything from there to the trailing
    'Tokens:' summary is the body.
    """
    if not stdout:
        return ""
    lines = stdout.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in _HEADER_PREFIXES):
            continue
        start = i
        break
    else:
        # All lines were header/blank — no body.
        return ""
    body_lines = lines[start:]
    while body_lines and (
        not body_lines[-1].strip()
        or any(body_lines[-1].strip().startswith(p) for p in _FOOTER_PREFIXES)
    ):
        body_lines.pop()
    return "\n".join(body_lines).strip()


_COST_RE = re.compile(r"\$([0-9]+\.?[0-9]*)\s+session")


def _parse_cost(stdout: str) -> float | None:
    """Extract the session cost ($X session) from aider's footer."""
    m = _COST_RE.search(stdout)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


_protocol_check: Harness = AiderHarness()  # noqa: F841


__all__ = [
    "AiderHarness",
    "AIDER_PATH_DEFAULT",
    "DEFAULT_AIDER_MODEL",
]
