"""Tests for remaining uncovered lines in bridge.py:
- post_init: per-chat command deletion
- _graceful_shutdown: remote proc timeout, photo dir errors
"""

import json
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge
import patchbay.projects
import patchbay.sessions


# ---------------------------------------------------------------------------
# post_init: per-chat command deletion with known chat IDs
# ---------------------------------------------------------------------------


class TestPostInitPerChatCommands:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(patchbay.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        monkeypatch.setattr(patchbay.sessions, "PENDING_DIR", tmp_path / "pending")
        (tmp_path / "pending").mkdir()
        monkeypatch.setattr(bridge, "PENDING_DIR", tmp_path / "pending")
        monkeypatch.setattr(bridge, "RESTART_NOTIFY_FILE", tmp_path / "restart.json")
        monkeypatch.setattr(bridge, "STALL_POLL_INTERVAL", 999)
        monkeypatch.setattr(bridge, "STALL_TIMEOUT", 999)
        bridge._bot_instance = None
        self._tmp = tmp_path

    @pytest.mark.asyncio
    async def test_deletes_per_chat_commands(self, monkeypatch):
        """When chat_projects.json has entries, post_init should attempt
        to delete commands per chat and handle failures gracefully."""
        cp_file = self._tmp / "cp.json"
        cp_file.write_text(json.dumps({"100_200": "project-a", "300_400": "project-b"}))
        monkeypatch.setattr(patchbay.projects, "CHAT_PROJECTS_FILE", cp_file)

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.set_my_commands = AsyncMock()
        call_count = [0]

        async def mock_delete(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 5:
                raise Exception("chat not found")

        bot.delete_my_commands = AsyncMock(side_effect=mock_delete)

        app_mock = MagicMock()
        app_mock.bot = bot

        with patch("bridge._stall_detector", new_callable=AsyncMock):
            await bridge.post_init(app_mock)

        assert bot.delete_my_commands.call_count >= 4


# ---------------------------------------------------------------------------
# _graceful_shutdown: remote proc wait timeout
# ---------------------------------------------------------------------------


class TestGracefulShutdownRemoteTimeout:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(patchbay.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        monkeypatch.setattr(bridge, "PHOTO_DIR", tmp_path / "photos")
        (tmp_path / "photos").mkdir()
        monkeypatch.setattr(bridge, "_sessions", {})
        bridge._shutting_down = False

    def test_remote_proc_wait_timeout_kills(self, monkeypatch):
        remote = MagicMock()
        remote.poll.return_value = None
        remote.wait.side_effect = subprocess.TimeoutExpired(cmd="", timeout=5)
        remote.pid = 9999
        monkeypatch.setattr(bridge, "_remote_proc", remote)

        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(14, None)

        remote.terminate.assert_called_once()
        remote.kill.assert_called_once()

    def test_photo_dir_error_suppressed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bridge, "_remote_proc", None)
        monkeypatch.setattr(bridge, "PHOTO_DIR", tmp_path / "nonexistent_photos")

        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(15, None)
