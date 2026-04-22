"""Comprehensive tests for Telegram command handlers in bridge.py."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge
import stargate.sessions


# ── Shared helpers ─────────────────────────────────────────────────────────


def _make_update(
    chat_id=1, thread_id=None, user_id=42, text="", title="TestChat"
):
    """Build a minimal mock Update for command handlers."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.title = title
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


# ── cmd_start ──────────────────────────────────────────────────────────────


class TestCmdStart:
    @pytest.mark.asyncio
    async def test_shows_user_id(self):
        update = _make_update(user_id=12345)
        ctx = _make_context()
        await bridge.cmd_start(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "12345" in reply

    @pytest.mark.asyncio
    async def test_lists_commands(self):
        update = _make_update()
        ctx = _make_context()
        await bridge.cmd_start(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "/clearnew" in reply
        assert "/ping" in reply
        assert "/kill" in reply


# ── cmd_clearnew ───────────────────────────────────────────────────────────


class TestCmdClearnew:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path)

    @pytest.mark.asyncio
    async def test_clears_session(self):
        bridge.save_session_id("1_2", "old-sess")
        update = _make_update(chat_id=1, thread_id=2)
        ctx = _make_context()
        await bridge.cmd_clearnew(update, ctx)
        assert bridge.get_session_id("1_2") is None

    @pytest.mark.asyncio
    async def test_replies_confirmation(self):
        update = _make_update()
        ctx = _make_context()
        await bridge.cmd_clearnew(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "Fresh session" in reply


# ── cmd_project ────────────────────────────────────────────────────────────


class TestCmdProject:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import stargate.projects

        monkeypatch.setattr(stargate.projects, "CHAT_PROJECTS_FILE", tmp_path / "cp.json")

    @pytest.mark.asyncio
    async def test_no_project_set(self):
        update = _make_update(chat_id=999, thread_id=1)
        ctx = _make_context()
        await bridge.cmd_project(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "No project set" in reply

    @pytest.mark.asyncio
    async def test_project_set(self):
        import stargate.projects

        stargate.projects._save_chat_projects({"999_1": "myproject"})
        update = _make_update(chat_id=999, thread_id=1)
        ctx = _make_context()
        await bridge.cmd_project(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "myproject" in reply

    @pytest.mark.asyncio
    async def test_project_with_agent(self):
        import stargate.projects

        stargate.projects._save_chat_projects(
            {"999_1": {"path": "Fanta", "agent": "plotter"}}
        )
        update = _make_update(chat_id=999, thread_id=1)
        ctx = _make_context()
        await bridge.cmd_project(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "Fanta" in reply
        assert "plotter" in reply


# ── cmd_setproject ─────────────────────────────────────────────────────────


class TestCmdSetproject:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import stargate.projects

        dev_dir = str(tmp_path / "dev")
        monkeypatch.setattr(stargate.projects, "CHAT_PROJECTS_FILE", tmp_path / "cp.json")
        monkeypatch.setattr(stargate.projects, "WORKING_DIR", dev_dir)
        monkeypatch.setattr(bridge, "WORKING_DIR", dev_dir)
        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        (tmp_path / "dev").mkdir()
        (tmp_path / "dev" / "project-a").mkdir()
        (tmp_path / "dev" / "project-b").mkdir()
        self._tmp = tmp_path

    @pytest.mark.asyncio
    async def test_with_valid_path(self):
        update = _make_update(chat_id=1, thread_id=2)
        ctx = _make_context(args=["project-a"])
        await bridge.cmd_setproject(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "project-a" in reply
        assert "Session reset" in reply

    @pytest.mark.asyncio
    async def test_with_invalid_path(self):
        update = _make_update(chat_id=1, thread_id=2)
        ctx = _make_context(args=["nonexistent"])
        await bridge.cmd_setproject(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply

    @pytest.mark.asyncio
    async def test_no_args_shows_picker(self):
        update = _make_update(chat_id=1, thread_id=2)
        ctx = _make_context(args=[])
        await bridge.cmd_setproject(update, ctx)
        call_kwargs = update.message.reply_text.call_args
        assert call_kwargs.kwargs.get("reply_markup") is not None

    @pytest.mark.asyncio
    async def test_setproject_clears_session(self):
        bridge.save_session_id("1_2", "old-session")
        update = _make_update(chat_id=1, thread_id=2)
        ctx = _make_context(args=["project-a"])
        await bridge.cmd_setproject(update, ctx)
        assert bridge.get_session_id("1_2") is None


# ── callback_setproject ────────────────────────────────────────────────────


class TestCallbackSetproject:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import stargate.projects

        dev_dir = str(tmp_path / "dev")
        monkeypatch.setattr(stargate.projects, "CHAT_PROJECTS_FILE", tmp_path / "cp.json")
        monkeypatch.setattr(stargate.projects, "WORKING_DIR", dev_dir)
        monkeypatch.setattr(bridge, "WORKING_DIR", dev_dir)
        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        (tmp_path / "dev").mkdir()
        (tmp_path / "dev" / "myrepo").mkdir()

    @pytest.mark.asyncio
    async def test_select_project(self):
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = "setproject:myrepo"
        update.callback_query.message.message_thread_id = 5
        update.effective_chat.id = 100
        update.effective_chat.title = "Test"
        ctx = _make_context()
        await bridge.callback_setproject(update, ctx)
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "myrepo" in text

    @pytest.mark.asyncio
    async def test_clear_project(self):
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = "setproject:__clear__"
        update.callback_query.message.message_thread_id = None
        update.effective_chat.id = 100
        ctx = _make_context()
        await bridge.callback_setproject(update, ctx)
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "cleared" in text.lower()

    @pytest.mark.asyncio
    async def test_invalid_project(self):
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = "setproject:nope"
        update.callback_query.message.message_thread_id = None
        update.effective_chat.id = 100
        ctx = _make_context()
        await bridge.callback_setproject(update, ctx)
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "not found" in text


# ── cmd_kill ───────────────────────────────────────────────────────────────


class TestCmdKill:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setattr(bridge, "_active_procs", {})

    @pytest.mark.asyncio
    async def test_kills_active_process(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 999
        bridge._active_procs["1_2"] = proc

        update = _make_update(chat_id=1, thread_id=2)
        ctx = _make_context()
        await bridge.cmd_kill(update, ctx)

        proc.kill.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "Killed" in reply

    @pytest.mark.asyncio
    async def test_no_active_process(self):
        update = _make_update(chat_id=1, thread_id=2)
        ctx = _make_context()
        await bridge.cmd_kill(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "No active" in reply


# ── cmd_ping ───────────────────────────────────────────────────────────────


class TestCmdPing:
    @pytest.fixture(autouse=True)
    def _isolate(self):
        bridge._processing_sessions.clear()
        bridge._session_start_times.clear()
        yield
        bridge._processing_sessions.clear()
        bridge._session_start_times.clear()

    @pytest.mark.asyncio
    async def test_no_active_sessions(self):
        update = _make_update()
        ctx = _make_context()
        await bridge.cmd_ping(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "pong" in reply
        assert "no active" in reply

    @pytest.mark.asyncio
    async def test_with_active_sessions(self):
        bridge._processing_sessions.add("1_2")
        bridge._session_start_times["1_2"] = time.time() - 65
        update = _make_update()
        ctx = _make_context()
        await bridge.cmd_ping(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "pong" in reply
        assert "1_2" in reply
        assert "1m" in reply


# ── cmd_restart ────────────────────────────────────────────────────────────


class TestCmdRestart:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "RESTART_NOTIFY_FILE", tmp_path / "restart.json")
        monkeypatch.setattr(bridge, "_active_procs", {})
        monkeypatch.setattr(bridge, "_remote_proc", None)
        self._tmp = tmp_path

    @pytest.mark.asyncio
    async def test_writes_restart_notify(self):
        update = _make_update(chat_id=123, thread_id=456)
        ctx = _make_context()
        with patch("os._exit") as mock_exit:
            await bridge.cmd_restart(update, ctx)
            mock_exit.assert_called_once_with(1)
        notify_file = self._tmp / "restart.json"
        assert notify_file.exists()
        data = json.loads(notify_file.read_text())
        assert data["chat_id"] == 123
        assert data["thread_id"] == 456

    @pytest.mark.asyncio
    async def test_terminates_active_procs(self):
        proc = MagicMock()
        proc.poll.return_value = None
        bridge._active_procs["test"] = proc
        update = _make_update()
        ctx = _make_context()
        with patch("os._exit"):
            await bridge.cmd_restart(update, ctx)
        proc.terminate.assert_called_once()
