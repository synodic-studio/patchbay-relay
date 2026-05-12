"""Shared pytest fixtures for patchbay tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest


# Production filesystem paths the bridge writes to at runtime. Tests must
# never touch these directly, otherwise a pytest run while the launchd
# bridge is live can wipe a real user's pending replies, pollute the real
# activity.jsonl, or signal/kill the real bridge process.
#
# Each entry is (logical_name, kind, [(module, attr), ...]) — every binding
# in the list is patched to the SAME tmp path so a value saved through one
# alias is visible through the others (bridge.save_pending writes via
# patchbay.sessions.PENDING_DIR; the test then reads via bridge.PENDING_DIR
# — both must resolve to the same dir).
#
# kind ∈ {"dir", "file"}: dir paths are pre-created; file paths are not.
_PRODUCTION_PATH_GROUPS = [
    (
        "session_dir",
        "dir",
        [
            ("patchbay.config", "SESSION_DIR"),
            ("patchbay.sessions", "SESSION_DIR"),
            ("bridge", "SESSION_DIR"),
        ],
    ),
    (
        "pending_dir",
        "dir",
        [
            ("patchbay.config", "PENDING_DIR"),
            ("patchbay.sessions", "PENDING_DIR"),
            ("bridge", "PENDING_DIR"),
        ],
    ),
    (
        "photo_dir",
        "dir",
        [
            ("patchbay.config", "PHOTO_DIR"),
            ("bridge", "PHOTO_DIR"),
        ],
    ),
    (
        "doc_dir",
        "dir",
        [
            ("patchbay.config", "DOC_DIR"),
            ("bridge", "DOC_DIR"),
        ],
    ),
    (
        "quarantine_dir",
        "dir",
        [("patchbay.config", "QUARANTINE_DIR")],
    ),
    (
        "activity_log",
        "file",
        [
            ("patchbay.config", "ACTIVITY_LOG"),
            ("patchbay.activity", "ACTIVITY_LOG"),
            ("bridge", "ACTIVITY_LOG"),
        ],
    ),
    (
        "chat_projects_file",
        "file",
        [
            ("patchbay.config", "CHAT_PROJECTS_FILE"),
            ("bridge", "CHAT_PROJECTS_FILE"),
        ],
    ),
    (
        "restart_notify_file",
        "file",
        [
            ("patchbay.config", "RESTART_NOTIFY_FILE"),
            ("bridge", "RESTART_NOTIFY_FILE"),
        ],
    ),
    (
        "lock_file",
        "file",
        [("patchbay.singleton", "LOCK_FILE")],
    ),
    (
        "mop_audit_dir",
        "dir",
        # Only patch the source. patchbay.harness.claude_sdk_mop reads through
        # `config.MOP_AUDIT_DIR` (not via a bound import), so a patch here
        # propagates without forcing the harness module to be imported during
        # every test's setup.
        [("patchbay.config", "MOP_AUDIT_DIR")],
    ),
]


@pytest.fixture(autouse=True)
def _isolate_production_paths(tmp_path, monkeypatch):
    """Redirect every production filesystem path to a tmp dir for this test.

    Without this, a test that wipes PENDING_DIR for cleanliness, writes to
    ACTIVITY_LOG via _log_activity, or invokes signal_other_bridge against
    LOCK_FILE will affect the real running bridge — including deleting a
    real user's queued reply or sending SIGTERM to the live process.
    """
    import importlib

    for name, kind, bindings in _PRODUCTION_PATH_GROUPS:
        target = tmp_path / name
        if kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        for module_path, attr in bindings:
            try:
                module = importlib.import_module(module_path)
            except ImportError:
                continue
            if not hasattr(module, attr):
                continue
            monkeypatch.setattr(module, attr, target, raising=False)


@pytest.fixture
def mock_bot():
    """Return a mock Telegram Application with a pre-wired bot.

    Provides async stubs for the most-used bot methods so tests that exercise
    bridge handlers don't need to set up real network connections.

    Usage::

        async def test_something(mock_bot):
            await bridge.some_handler(update, mock_bot["context"])
            mock_bot["bot"].send_message.assert_called_once()
    """
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_chat_action = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_document = AsyncMock()
    bot.answer_callback_query = AsyncMock()

    application = MagicMock()
    application.bot = bot

    context = MagicMock()
    context.bot = bot
    context.application = application

    return {
        "bot": bot,
        "application": application,
        "context": context,
    }
