"""Tests for cmd_setproject inline keyboard project listing."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import bridge


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

    def test_keyboard_contains_all_projects(self):
        projects = ["Alpha", "Bravo", "Charlie"]
        update = _make_update()
        ctx = _make_context()

        with patch.object(bridge, "_get_all_projects", return_value=projects):
            asyncio.run(bridge.cmd_setproject(update, ctx))

        update.message.reply_text.assert_called_once()
        markup = update.message.reply_text.call_args.kwargs.get("reply_markup")
        assert markup is not None

        button_texts = [row[0].text for row in markup.inline_keyboard]
        assert button_texts == ["Alpha", "Bravo", "Charlie", "Clear (use ~/Developer)"]

    def test_keyboard_callback_data_matches_project_names(self):
        projects = ["Fanta", "patchbay-relay"]
        update = _make_update()
        ctx = _make_context()

        with patch.object(bridge, "_get_all_projects", return_value=projects):
            asyncio.run(bridge.cmd_setproject(update, ctx))

        markup = update.message.reply_text.call_args.kwargs.get("reply_markup")
        callback_data = [row[0].callback_data for row in markup.inline_keyboard]
        assert callback_data == [
            "setproject:Fanta",
            "setproject:patchbay-relay",
            "setproject:__clear__",
        ]

    def test_keyboard_empty_project_list_still_has_clear(self):
        update = _make_update()
        ctx = _make_context()

        with patch.object(bridge, "_get_all_projects", return_value=[]):
            asyncio.run(bridge.cmd_setproject(update, ctx))

        markup = update.message.reply_text.call_args.kwargs.get("reply_markup")
        assert len(markup.inline_keyboard) == 1
        assert markup.inline_keyboard[0][0].callback_data == "setproject:__clear__"

    def test_does_not_show_keyboard_when_args_given(self):
        """With args, cmd_setproject should try to set the project, not show the picker."""
        update = _make_update()
        ctx = _make_context(args=["some-project"])

        with patch("os.path.isdir", return_value=False):
            asyncio.run(bridge.cmd_setproject(update, ctx))

        markup = update.message.reply_text.call_args.kwargs.get("reply_markup")
        assert markup is None
