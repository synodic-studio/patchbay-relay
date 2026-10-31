"""Subscription-authenticated Codex CLI harness with JSONL activity streaming."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from ..config import MAX_TIMEOUT, logger
from .base import HarnessCapabilities, TextDelta, ToolResult, ToolUse, TurnError, TurnEvent, TurnFinal, TurnRequest

CODEX_PATH_DEFAULT = shutil.which("codex") or "/opt/homebrew/bin/codex"
_RATE_LIMIT_TOKENS = ("rate limit", "rate_limit", "quota", "too many requests", "429", "usage limit", "limit reached")


@dataclass
class _Exit:
    code: int
    stderr: str
    duration: float
    timed_out: bool = False


class CodexHarness:
    """Run persisted Codex turns using the user's existing ChatGPT login.

    Tool activity streams as it arrives. Only an explicit turn.completed and a
    zero process exit together constitute success. Billing totals are absent
    from Codex's protocol, so total_cost_usd remains unknown.
    """

    name = "codex"
    capabilities = HarnessCapabilities(
        supports_resume=True,
        supports_tool_streaming=True,
        supports_interrupt=False,
        supports_effort=True,
        supports_mcp=True,
    )

    def __init__(
        self,
        *,
        codex_path: str = CODEX_PATH_DEFAULT,
        max_timeout_seconds: float = MAX_TIMEOUT,
        on_progress: Callable[[], None] | None = None,
        proc_setter: Callable[[subprocess.Popen | None], None] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self._codex_path = codex_path
        self._max_timeout = max_timeout_seconds
        self._on_progress = on_progress
        self._proc_setter = proc_setter
        self._env_overrides = env_overrides or {}
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False

    def _build_cmd(self, req: TurnRequest) -> list[str]:
        cmd = [self._codex_path, "exec"]
        if req.resume_session_id:
            cmd.append("resume")
        cmd += ["--json", "--skip-git-repo-check"]
        config = {
            "approval_policy": "never",
            "sandbox_mode": "danger-full-access",
            "forced_login_method": "chatgpt",
            "model_provider": "openai",
        }
        if req.system_prompt:
            config["developer_instructions"] = req.system_prompt
        if req.effort:
            config["model_reasoning_effort"] = "xhigh" if req.effort == "max" else req.effort
        for key, value in config.items():
            cmd += ["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"]
        if req.model:
            cmd += ["--model", req.model]
        if req.resume_session_id:
            cmd.append(req.resume_session_id)
        return cmd + ["-"]

    async def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict | _Exit | TurnError] = asyncio.Queue()
        self._cancelled = False

        def emit(value):
            loop.call_soon_threadsafe(queue.put_nowait, value)

        worker = loop.run_in_executor(None, self._run, req, emit)
        session_id = req.resume_session_id
        final_text = ""
        completed = 0
        errors: list[str] = []
        turn_failed = False
        tool_ids: set[str] = set()
        try:
            while True:
                event = await queue.get()
                if isinstance(event, TurnError):
                    yield event
                    return
                if isinstance(event, _Exit):
                    error_text = "\n".join(errors) or event.stderr.strip()
                    metadata = {
                        "session_id": session_id,
                        "exit_code": event.code,
                        "duration": event.duration,
                        "stderr": event.stderr[:500],
                    }
                    if event.timed_out:
                        yield TurnError("timeout", "Codex turn timed out", True, metadata)
                    elif turn_failed or event.code != 0 or not completed or self._cancelled:
                        message = error_text or "Codex exited without a completed turn"
                        lower = message.lower()
                        kind = "process_died"
                        if any(token in lower for token in _RATE_LIMIT_TOKENS):
                            kind = "rate_limit"
                        elif event.code in (137, -9) and not self._cancelled:
                            kind = "oom"
                        elif req.resume_session_id and any(
                            token in lower
                            for token in (
                                "no session found",
                                "session not found",
                                "no conversation found",
                                "failed to load session",
                            )
                        ):
                            kind = "corrupt_session"
                        yield TurnError(kind, message[:500], kind in ("rate_limit", "corrupt_session", "oom"), metadata)
                    else:
                        text = final_text.strip() or "(Codex completed without a text response.)"
                        yield TextDelta(text, final=True)
                        yield TurnFinal(session_id, completed, None, text)
                    return
                event_type = event.get("type")
                if event_type == "thread.started":
                    session_id = event.get("thread_id") or session_id
                elif event_type == "turn.completed":
                    completed += 1
                elif event_type in ("turn.failed", "error"):
                    if event_type == "turn.failed":
                        turn_failed = True
                    error = event.get("error") or event.get("message") or "Codex turn failed"
                    errors.append(str(error.get("message", error)) if isinstance(error, dict) else str(error))
                elif event_type in ("item.started", "item.completed"):
                    item = event.get("item")
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    item_id = item.get("id")
                    if item_type == "agent_message" and event_type == "item.completed":
                        if isinstance(item.get("text"), str):
                            final_text = item["text"]
                    elif item_type in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
                        if item_id not in tool_ids:
                            tool_ids.add(item_id)
                            name = item_type
                            inputs = {key: item[key] for key in ("command", "query", "changes") if key in item}
                            if item_type == "mcp_tool_call":
                                name = f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
                                inputs = item.get("arguments") or {}
                            yield ToolUse(name, inputs, item_id)
                        if event_type == "item.completed":
                            output = item.get("aggregated_output", item.get("result", item.get("error", "")))
                            if not isinstance(output, str):
                                output = json.dumps(output, ensure_ascii=False)
                            failed = item.get("status") == "failed" or item.get("exit_code") not in (None, 0)
                            yield ToolResult(item_id, output[:4000], failed)
        finally:
            if not worker.done():
                await self.cancel()
            await asyncio.shield(worker)

    async def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None:
            self._kill(proc)
            await asyncio.to_thread(proc.wait)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _callback(self, callback, *args) -> None:
        if callback:
            try:
                callback(*args)
            except Exception:  # noqa: BLE001
                logger.exception("Codex harness callback raised")

    def _run(self, req: TurnRequest, emit: Callable) -> None:
        start = time.monotonic()
        env = os.environ.copy()
        env.update(self._env_overrides)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(key, None)
        try:
            proc = subprocess.Popen(
                self._build_cmd(req),
                cwd=str(req.project_dir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            emit(TurnError("process_died", f"Cannot start Codex: {exc}", False))
            return
        with self._lock:
            self._proc = proc
            cancelled = self._cancelled
        self._callback(self._proc_setter, proc)
        if cancelled:
            self._kill(proc)
        stderr: list[str] = []

        def read_stdout():
            try:
                for line in proc.stdout:
                    self._callback(self._on_progress)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        emit(event)
            finally:
                proc.stdout.close()

        def read_stderr():
            try:
                for line in proc.stderr:
                    stderr.append(line)
            finally:
                proc.stderr.close()

        def write_stdin():
            try:
                proc.stdin.write(req.prompt)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        threads = [threading.Thread(target=target, daemon=True) for target in (read_stdout, read_stderr, write_stdin)]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            proc.wait(timeout=self._max_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(proc)
            proc.wait()
        finally:
            # Descendants may retain the pipe handles after the CLI exits.
            self._kill(proc)
            for thread in threads:
                thread.join(timeout=2)
            with self._lock:
                self._proc = None
            self._callback(self._proc_setter, None)
        emit(_Exit(proc.returncode, "".join(stderr), time.monotonic() - start, timed_out))
