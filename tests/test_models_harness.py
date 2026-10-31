"""Harness-specific model choices must survive switching without leaking."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bridge  # noqa: F401 - initialize command re-exports before importing commands
from patchbay import models, projects
from patchbay.commands import project


@pytest.fixture(autouse=True)
def isolate_models(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "CHAT_MODELS_FILE", tmp_path / "chat_models.json")
    monkeypatch.setattr(projects, "CHAT_PROJECTS_FILE", tmp_path / "chat_projects.json")
    monkeypatch.setattr(models, "CODEX_MODEL", None, raising=False)


def test_model_preferences_are_independent_for_each_harness():
    models.set_chat_model("11_22", "dsf")
    assert models.resolve_model("11_22", harness="codex") is None
    models.set_chat_model("11_22", "gpt-5.4", harness="codex")
    assert models.resolve_model("11_22") == "dsf"
    assert models.resolve_model("11_22", harness="codex") == "gpt-5.4"
    models.set_chat_model("11_22", None, harness="codex")
    assert models.resolve_model("11_22") == "dsf"
    assert models.resolve_model("11_22", harness="codex") is None


def test_codex_default_uses_its_environment_setting(monkeypatch):
    monkeypatch.setattr(models, "CODEX_MODEL", "gpt-5.4")
    assert models.resolve_model("11_22", harness="codex") == "gpt-5.4"
    assert models.resolve_model("11_22") == "small"


def test_pi_shortcut_is_left_in_codex_prompt():
    assert models.extract_model_prefix("!s explain", harness="codex") == (None, "!s explain")
    assert models.extract_model_prefix("!s explain") == ("small", "explain")


def make_update(args):
    message = SimpleNamespace(message_thread_id=22, reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=11))
    return update, SimpleNamespace(args=args)


@pytest.mark.asyncio
async def test_codex_command_sets_model_without_overwriting_pi_choice():
    projects.set_chat_harness("11_22", "codex")
    models.set_chat_model("11_22", "dsf")
    update, context = make_update(["gpt-5.4"])
    await project.cmd_model(update, context)
    assert models.get_chat_model("11_22", harness="codex") == "gpt-5.4"
    assert models.get_chat_model("11_22") == "dsf"


@pytest.mark.asyncio
async def test_codex_command_rejects_pi_alias():
    projects.set_chat_harness("11_22", "codex")
    update, context = make_update(["small"])
    await project.cmd_model(update, context)
    assert models.get_chat_model("11_22", harness="codex") is None
    assert "Invalid" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_codex_picker_does_not_call_pi(monkeypatch):
    projects.set_chat_harness("11_22", "codex")

    def forbidden():
        pytest.fail("Codex model picker queried pi")

    monkeypatch.setattr(models, "list_available_models", forbidden)
    update, context = make_update([])
    await project.cmd_model(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert "Codex" in text
    assert "/model" in text


@pytest.mark.asyncio
async def test_stale_pi_picker_cannot_apply_alias_to_codex():
    projects.set_chat_harness("11_22", "codex")
    query = SimpleNamespace(
        answer=AsyncMock(), edit_message_text=AsyncMock(),
        message=SimpleNamespace(message_thread_id=22), data="model:small",
    )
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=11))
    await project.callback_model(update, SimpleNamespace())
    assert models.get_chat_model("11_22", harness="codex") is None
    assert "Invalid" in query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_switching_harness_clears_session_but_preserves_models(monkeypatch):
    from patchbay.sessions import get_session_id, save_session_id

    monkeypatch.setattr(project, "VALID_HARNESSES", ("pi", "codex"))
    models.set_chat_model("11_22", "dsf")
    save_session_id("11_22", "old-pi")
    update, context = make_update(["codex"])
    await project.cmd_harness(update, context)
    assert projects.get_chat_harness("11_22") == "codex"
    assert get_session_id("11_22") is None
    assert models.get_chat_model("11_22") == "dsf"


@pytest.mark.asyncio
async def test_selecting_current_harness_preserves_session():
    from patchbay.sessions import get_session_id, save_session_id

    save_session_id("11_22", "pi-current")
    update, context = make_update(["pi"])
    await project.cmd_harness(update, context)
    assert get_session_id("11_22") == "pi-current"


@pytest.mark.asyncio
async def test_resetting_codex_to_default_clears_session():
    from patchbay.sessions import get_session_id, save_session_id

    projects.set_chat_harness("11_22", "codex")
    save_session_id("11_22", "old-codex")
    update, context = make_update(["default"])
    await project.cmd_harness(update, context)
    assert projects.get_chat_harness("11_22") is None
    assert get_session_id("11_22") is None


@pytest.mark.asyncio
async def test_fresh_codex_session_reports_codex_model():
    projects.set_chat_harness("11_22", "codex")
    models.set_chat_model("11_22", "dsf")
    models.set_chat_model("11_22", "gpt-5.4", harness="codex")
    update, context = make_update([])
    await bridge.cmd_clearnew(update, context)
    text = update.message.reply_text.call_args.args[0]
    assert "gpt-5.4" in text
    assert "dsf" not in text
