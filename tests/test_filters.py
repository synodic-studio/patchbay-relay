"""Tests for silence-narration and noisy-status response filters."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _import_bridge():
    import bridge  # noqa: F401 — ensures telegram_send is fully wired


# --- Silence narration ---


@pytest.mark.parametrize(
    "text",
    [
        "*(silent)*",
        "*(silence)*",
        "(silent)",
        "(silence)",
        "🔇",
        "...",
        "…",
        "  (silent)  ",
        "*silent*",
        "_(no response)_",
        "(no reply)",
    ],
)
def test_silence_narration_matches(text):
    from patchbay.telegram_send import _is_silence_narration

    assert _is_silence_narration(text)


@pytest.mark.parametrize(
    "text",
    [
        "I completed the task.",
        "Silent operation complete.",
        "The system is silent now.",
        "(silent) but there's more here that exceeds 64 chars and should not be filtered at all",
        "",
        "   ",
        "a" * 65,
    ],
)
def test_silence_narration_no_match(text):
    from patchbay.telegram_send import _is_silence_narration

    assert not _is_silence_narration(text)


# --- Noisy status ---


@pytest.mark.parametrize(
    "text",
    [
        "Compacting context — summarizing earlier conversation",
        "compacting context - summarizing the session",
        "Rate limited, waiting 3s",
        "rate limited. waiting 5",
        "Retrying in 3s",
        "retrying in 10s",
    ],
)
def test_noisy_status_matches(text):
    from patchbay.telegram_send import _is_noisy_status

    assert _is_noisy_status(text)


@pytest.mark.parametrize(
    "text",
    [
        "I ran the tests and all passed.",
        "Context is getting large, consider /compact.",
        "I've compacted my notes for clarity.",
        "",
        "Rate limited. waiting 3s but this is a longer message that describes the situation in detail",
        "a" * 201,
    ],
)
def test_noisy_status_no_match(text):
    from patchbay.telegram_send import _is_noisy_status

    assert not _is_noisy_status(text)
