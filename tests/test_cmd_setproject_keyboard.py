"""Tests for cmd_setproject inline keyboard project listing."""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Stub heavy dependencies so bridge.py can be imported without side effects.
def _stub_modules():
    """Pre-populate sys.modules with lightweight stubs for bridge's imports."""
    # dotenv
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **kw: None
    sys.modules.setdefault("dotenv", dotenv)

    # auth (local module)
    auth_mod = types.ModuleType("auth")
    auth_mod.is_user_approved = lambda uid: True
    sys.modules.setdefault("auth", auth_mod)


_stub_modules()


@pytest.fixture()
def _patch_bridge_init():
    """Patch subprocess and env so bridge module-level init doesn't hit real system."""
    fake_result = MagicMock(returncode=0, stdout="fake-token\n")
    with (
        patch("subprocess.run", return_value=fake_result),
        patch.dict(
            "os.environ",
            {"CLAUDE_WORKING_DIR": "/tmp/fake-developer", "TELEGRAM_BOT_TOKEN": "fake"},
        ),
    ):
        # Force re-import so patched subprocess.run is used for BOT_TOKEN init
        sys.modules.pop("bridge", None)
        import bridge  # noqa: F811

        yield bridge

    sys.modules.pop("bridge", None)


@pytest.fixture()
def bridge_mod(_patch_bridge_init):
    return _patch_bridge_init


def _make_update(chat_id=123, thread_id=None):
    """Build a minimal mock Update for cmd_setproject."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.message_thread_id = thread_id
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


class TestCmdSetprojectKeyboard:
    """cmd_setproject with no args should show an inline keyboard listing all projects."""

    def test_keyboard_contains_all_projects(self, bridge_mod):
        projects = ["Alpha", "Bravo", "Charlie"]
        update = _make_update()
        ctx = _make_context()

        with patch.object(bridge_mod, "_get_all_projects", return_value=projects):
            asyncio.run(bridge_mod.cmd_setproject(update, ctx))

        update.message.reply_text.assert_called_once()
        call_kwargs = update.message.reply_text.call_args
        markup = call_kwargs.kwargs.get("reply_markup") or call_kwargs[1].get(
            "reply_markup"
        )

        # Each project should appear as a button row, plus one "Clear" row
        button_texts = [row[0].text for row in markup.inline_keyboard]
        assert button_texts == ["Alpha", "Bravo", "Charlie", "Clear (use ~/Developer)"]

    def test_keyboard_callback_data_matches_project_names(self, bridge_mod):
        projects = ["Fanta", "patchbay-relay"]
        update = _make_update()
        ctx = _make_context()

        with patch.object(bridge_mod, "_get_all_projects", return_value=projects):
            asyncio.run(bridge_mod.cmd_setproject(update, ctx))

        call_kwargs = update.message.reply_text.call_args
        markup = call_kwargs.kwargs.get("reply_markup") or call_kwargs[1].get(
            "reply_markup"
        )

        callback_data = [row[0].callback_data for row in markup.inline_keyboard]
        assert callback_data == [
            "setproject:Fanta",
            "setproject:patchbay-relay",
            "setproject:__clear__",
        ]

    def test_keyboard_empty_project_list_still_has_clear(self, bridge_mod):
        update = _make_update()
        ctx = _make_context()

        with patch.object(bridge_mod, "_get_all_projects", return_value=[]):
            asyncio.run(bridge_mod.cmd_setproject(update, ctx))

        call_kwargs = update.message.reply_text.call_args
        markup = call_kwargs.kwargs.get("reply_markup") or call_kwargs[1].get(
            "reply_markup"
        )

        assert len(markup.inline_keyboard) == 1
        assert markup.inline_keyboard[0][0].callback_data == "setproject:__clear__"

    def test_does_not_show_keyboard_when_args_given(self, bridge_mod):
        """With args, cmd_setproject should try to set the project, not show the picker."""
        update = _make_update()
        ctx = _make_context(args=["some-project"])

        with patch("os.path.isdir", return_value=False):
            asyncio.run(bridge_mod.cmd_setproject(update, ctx))

        call_kwargs = update.message.reply_text.call_args
        # Should NOT have reply_markup (no inline keyboard)
        markup = call_kwargs.kwargs.get("reply_markup") or (
            call_kwargs[1].get("reply_markup") if len(call_kwargs) > 1 else None
        )
        assert markup is None
