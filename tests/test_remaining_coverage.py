"""Tests for remaining uncovered lines in bridge.py:
- cmd_restart unauthorized user (line 907)
- cmd_remote_control: timeout on old proc kill (968-969), _read_initial_output (1000-1004),
  remote-control still running path (1024-1025)
- post_init: per-chat command deletion (1169-1172)
- _graceful_shutdown: remote proc timeout (1242-1243)
"""

import json
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge
import stargate.projects
import stargate.sessions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_update(chat_id=1, thread_id=None, user_id=42, text=""):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args=None):
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.args = args or []
    return ctx


# ---------------------------------------------------------------------------
# cmd_restart unauthorized
# ---------------------------------------------------------------------------


class TestCmdRestartUnauthorized:
    @pytest.mark.asyncio
    async def test_unauthorized_user_silently_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", {9999})
        monkeypatch.setattr(bridge, "RESTART_NOTIFY_FILE", tmp_path / "restart.json")
        update = _make_update(user_id=42)
        ctx = _make_context()
        await bridge.cmd_restart(update, ctx)
        update.message.reply_text.assert_not_called()


# ---------------------------------------------------------------------------
# cmd_remote_control: old proc kill timeout path
# ---------------------------------------------------------------------------


class TestCmdRemoteControlTimeout:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "_remote_proc", None)
        monkeypatch.setattr(bridge, "_remote_proc_key", None)
        monkeypatch.setattr(
            stargate.projects, "CHAT_PROJECTS_FILE", tmp_path / "cp.json"
        )

    @pytest.mark.asyncio
    async def test_old_proc_kill_on_wait_timeout(self, monkeypatch):
        """When the old remote proc doesn't respond to terminate within timeout,
        it should be killed."""
        old_proc = MagicMock()
        old_proc.poll.return_value = None
        old_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="", timeout=5)
        old_proc.pid = 1111
        monkeypatch.setattr(bridge, "_remote_proc", old_proc)
        monkeypatch.setattr(bridge, "_remote_proc_key", "old")

        new_proc = MagicMock()
        new_proc.poll.return_value = 0
        new_proc.returncode = 0
        new_proc.pid = 2222
        new_proc.stdout = MagicMock()
        new_proc.stdout.read.return_value = ""
        new_proc.stdout.readline.return_value = ""

        with patch("bridge.subprocess.Popen", return_value=new_proc):
            update = _make_update(text="/remote_control")
            ctx = _make_context()
            await bridge.cmd_remote_control(update, ctx)

        old_proc.terminate.assert_called_once()
        old_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_remote_control: process still running after initial output
# ---------------------------------------------------------------------------


class TestCmdRemoteControlStillRunning:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "ALLOWED_USER_IDS", set())
        monkeypatch.setattr(bridge, "_remote_proc", None)
        monkeypatch.setattr(bridge, "_remote_proc_key", None)
        monkeypatch.setattr(
            stargate.projects, "CHAT_PROJECTS_FILE", tmp_path / "cp.json"
        )

    @pytest.mark.asyncio
    async def test_process_still_running_shows_pid(self, monkeypatch):
        """When remote-control is still running after reading initial output,
        it should show the running message with PID."""
        import io

        new_proc = MagicMock()
        # poll() returns None (still running) for all checks
        new_proc.poll.return_value = None
        new_proc.pid = 5555
        new_proc.stdout = io.StringIO("")

        with (
            patch("bridge.subprocess.Popen", return_value=new_proc),
            # select returns nothing ready, so the loop just waits until deadline
            patch("bridge.select.select", return_value=([], [], [])),
        ):
            update = _make_update(text="/remote_control")
            ctx = _make_context()
            await bridge.cmd_remote_control(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "running" in reply.lower()
        assert "5555" in reply
        assert "/remote stop" in reply

    @pytest.mark.asyncio
    async def test_process_outputs_lines_while_running(self, monkeypatch):
        """When remote-control outputs some lines and stays running."""

        new_proc = MagicMock()
        new_proc.poll.return_value = None
        new_proc.pid = 6666
        # stdout.readline returns ANSI-wrapped lines then empty
        readline_results = iter(
            [
                "\x1b[32mSession URL: https://example.com\x1b[0m\n",
                "",
            ]
        )
        new_proc.stdout = MagicMock()
        new_proc.stdout.readline = MagicMock(side_effect=lambda: next(readline_results, ""))

        select_results = iter(
            [
                ([new_proc.stdout], [], []),
                ([new_proc.stdout], [], []),
                ([], [], []),  # no more data
            ]
        )

        with (
            patch("bridge.subprocess.Popen", return_value=new_proc),
            patch("bridge.select.select", side_effect=lambda *a, **kw: next(select_results, ([], [], []))),
        ):
            update = _make_update(text="/remote_control")
            ctx = _make_context()
            await bridge.cmd_remote_control(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Session URL" in reply
        assert "6666" in reply


# ---------------------------------------------------------------------------
# post_init: per-chat command deletion with known chat IDs
# ---------------------------------------------------------------------------


class TestPostInitPerChatCommands:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        monkeypatch.setattr(stargate.sessions, "PENDING_DIR", tmp_path / "pending")
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
        monkeypatch.setattr(stargate.projects, "CHAT_PROJECTS_FILE", cp_file)

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.set_my_commands = AsyncMock()
        # Make delete succeed for first chat, fail for second
        call_count = [0]

        async def mock_delete(*args, **kwargs):
            call_count[0] += 1
            # Let some fail to test the except branch
            if call_count[0] > 5:
                raise Exception("chat not found")

        bot.delete_my_commands = AsyncMock(side_effect=mock_delete)

        app_mock = MagicMock()
        app_mock.bot = bot

        with patch("bridge._stall_detector", new_callable=AsyncMock):
            await bridge.post_init(app_mock)

        # Should have tried to delete for generic scopes (4) + per-chat (2 unique chat IDs)
        assert bot.delete_my_commands.call_count >= 4


# ---------------------------------------------------------------------------
# _graceful_shutdown: remote proc wait timeout
# ---------------------------------------------------------------------------


class TestGracefulShutdownRemoteTimeout:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        monkeypatch.setattr(bridge, "PHOTO_DIR", tmp_path / "photos")
        (tmp_path / "photos").mkdir()
        monkeypatch.setattr(bridge, "_active_procs", {})
        bridge._shutting_down = False

    def test_remote_proc_wait_timeout_kills(self, monkeypatch):
        """When remote proc doesn't respond to terminate within timeout,
        it should be killed."""
        remote = MagicMock()
        remote.poll.return_value = None
        remote.wait.side_effect = subprocess.TimeoutExpired(cmd="", timeout=5)
        remote.pid = 9999
        monkeypatch.setattr(bridge, "_remote_proc", remote)

        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(14, None)  # 14 = SIGALRM, valid signal number

        remote.terminate.assert_called_once()
        remote.kill.assert_called_once()

    def test_photo_dir_error_suppressed(self, monkeypatch, tmp_path):
        """If cleaning photo dir raises, shutdown should continue."""
        monkeypatch.setattr(bridge, "_remote_proc", None)

        # Make PHOTO_DIR point to a non-existent path that will raise
        monkeypatch.setattr(bridge, "PHOTO_DIR", tmp_path / "nonexistent_photos")

        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(15, None)

        # Should exit cleanly despite the error
