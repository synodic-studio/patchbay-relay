"""Tests for the client-side context estimator (pi has no token query).

pi exposes no token/context readout (`pi --help` has no such flag; its events
carry only usage.cost), so /context is filled by estimating tokens over the
session transcript against the active model's window. litellm (already a dep)
provides both the token count and the window, with heuristic fallbacks for
models it doesn't know.
"""

from __future__ import annotations

from patchbay.harness.base import ContextUsage
from patchbay.harness.context_estimate import estimate_context_usage


def test_returns_context_usage_with_percentage():
    cu = estimate_context_usage("gpt-4o", "hello world " * 10, fallback_window=100_000)
    assert isinstance(cu, ContextUsage)
    assert cu.used_tokens > 0
    assert cu.max_tokens > 0
    assert 0.0 <= cu.percentage <= 100.0
    assert cu.model == "gpt-4o"


def test_unknown_model_still_counts_but_falls_back_on_window():
    # litellm.token_counter counts even unknown models (default tokenizer), so
    # used_tokens is a real count; only the window can't be resolved and falls
    # back to the caller-supplied default.
    text = "x" * 4000
    cu = estimate_context_usage("totally-made-up/model-xyz", text, fallback_window=8_000)
    assert cu.max_tokens == 8_000
    assert cu.used_tokens > 0
    assert cu.percentage == round(cu.used_tokens / 8_000 * 100, 1)


def test_heuristic_fallback_when_litellm_counter_raises(monkeypatch):
    # If litellm.token_counter itself blows up, fall back to ~4 chars/token
    # rather than raising out of a /context command.
    import litellm

    monkeypatch.setattr(litellm, "token_counter", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    cu = estimate_context_usage("nope/nope", "z" * 4000, fallback_window=8_000)
    assert 900 <= cu.used_tokens <= 1100  # ~len/4


def test_percentage_clamps_at_100():
    # Far more text than the window still reports <= 100%.
    cu = estimate_context_usage("nope/nope", "y" * 40_000, fallback_window=1_000)
    assert cu.percentage == 100.0


def test_empty_transcript_is_zero_usage():
    cu = estimate_context_usage("nope/nope", "", fallback_window=1_000)
    assert cu.used_tokens == 0
    assert cu.percentage == 0.0
