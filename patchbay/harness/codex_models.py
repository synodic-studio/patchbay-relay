"""Discover picker models using Codex's authenticated app-server protocol.

No model turn is started. The CLI owns credential storage and refresh; this
module only checks ChatGPT account mode and reads its visible model catalog.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time

from .codex import CODEX_PATH_DEFAULT


class CodexModelsUnavailable(RuntimeError):
    """The authenticated Codex model picker cannot currently be populated."""


class _Connection:
    def __init__(self, process: subprocess.Popen, deadline: float) -> None:
        self.process = process
        self.deadline = deadline
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        self.selector.register(process.stderr, selectors.EVENT_READ)
        self.buffer = b""
        self.request_id = 0

    def send(self, method: str, params: dict, request_id: int | None = None) -> None:
        message = {"method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.request_id += 1
        self.send(method, params, self.request_id)
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise CodexModelsUnavailable("Codex model discovery timed out. Try /model again.")
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CodexModelsUnavailable(
                        "Codex returned an unreadable model catalog. Try /model again."
                    ) from exc
                if not isinstance(message, dict) or message.get("id") != self.request_id:
                    continue
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise CodexModelsUnavailable("Codex could not load its model catalog. Try /model again.")
                return message["result"]
            ready = self.selector.select(remaining)
            if not ready:
                raise CodexModelsUnavailable("Codex model discovery timed out. Try /model again.")
            for key, _ in ready:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.fileobj is self.process.stdout:
                        raise CodexModelsUnavailable(
                            "Codex stopped before returning its model catalog. Try /model again."
                        )
                elif key.fileobj is self.process.stdout:
                    self.buffer += chunk

    def close(self) -> None:
        self.selector.close()


def list_codex_models(*, codex_path: str = CODEX_PATH_DEFAULT, timeout_seconds: float = 15) -> list[tuple[str, str]]:
    """Return visible model slugs and labels for the current ChatGPT login.

    Fail closed if subscription auth or the catalog cannot be verified. CLI
    credentials remain private; API key authentication is never a fallback.
    """
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)
    process = None
    connection = None
    try:
        process = subprocess.Popen(
            [
                codex_path,
                "app-server",
                "--stdio",
                "-c",
                'forced_login_method="chatgpt"',
                "-c",
                'model_provider="openai"',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        connection = _Connection(process, time.monotonic() + timeout_seconds)
        connection.request(
            "initialize", {"clientInfo": {"name": "patchbay_relay", "title": "Patchbay Relay", "version": "0.2.0"}}
        )
        connection.send("initialized", {})
        account = connection.request("account/read", {"refreshToken": False}).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise CodexModelsUnavailable(
                "Codex needs an existing ChatGPT subscription login before models can be listed."
            )
        models: list[tuple[str, str]] = []
        seen: set[str] = set()
        cursors: set[str] = set()
        cursor = None
        while True:
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            page = connection.request("model/list", params)
            data = page.get("data")
            if not isinstance(data, list):
                raise CodexModelsUnavailable("Codex returned an unreadable model catalog. Try /model again.")
            for entry in data:
                if not isinstance(entry, dict) or entry.get("hidden"):
                    continue
                model = entry.get("model")
                if not isinstance(model, str) or not model or model in seen:
                    continue
                if len(("model:codex:" + model).encode("utf-8")) > 64:
                    continue
                label = entry.get("displayName")
                models.append((model, label if isinstance(label, str) and label else model))
                seen.add(model)
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise CodexModelsUnavailable("Codex returned an unreadable model catalog. Try /model again.")
            cursors.add(cursor)
        if not models:
            raise CodexModelsUnavailable(
                "Codex returned no selectable models for this ChatGPT account. Try /model again."
            )
        return models
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexModelsUnavailable(
            "Codex model discovery is unavailable. Check the CLI and ChatGPT login, then try /model again."
        ) from exc
    finally:
        if connection:
            connection.close()
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
