"""Client-side context-window estimation.

pi exposes no token or context-window readout (`pi --help` has no such flag,
and its JSON events carry only usage.cost, not token counts). So `/context`
for the pi harness is filled by estimating tokens over the session transcript
against the active model's window.

litellm is already a dependency and knows token counts and context windows for
many providers (OpenAI, Anthropic, DeepSeek, ...), so we use it when it can
resolve the model, and fall back to a cheap heuristic (~4 chars per token) plus
a caller-supplied default window otherwise. The result is always an estimate;
callers should present it as such.
"""

from __future__ import annotations

from .base import ContextUsage

# Rough bytes-per-token used only when litellm can't count for a model.
_HEURISTIC_CHARS_PER_TOKEN = 4


def _count_tokens(model: str, text: str) -> int:
    if not text:
        return 0
    try:
        import litellm

        n = litellm.token_counter(model=model, text=text)
        if isinstance(n, int) and n > 0:
            return n
    except Exception:
        pass
    # Heuristic fallback: ~4 characters per token, at least 1 for non-empty text.
    return max(1, round(len(text) / _HEURISTIC_CHARS_PER_TOKEN))


def _window(model: str, fallback_window: int) -> int:
    try:
        import litellm

        mx = litellm.get_max_tokens(model)
        if isinstance(mx, int) and mx > 0:
            return mx
    except Exception:
        pass
    return fallback_window


def estimate_context_usage(model: str, transcript: str, *, fallback_window: int = 200_000) -> ContextUsage:
    """Estimate context usage for *transcript* under *model*.

    Uses litellm for the token count and window when it knows the model,
    otherwise a ~4-chars-per-token heuristic and *fallback_window*. Percentage
    is clamped to 0..100.
    """
    used = _count_tokens(model, transcript)
    mx = _window(model, fallback_window)
    pct = round(min(100.0, used / mx * 100), 1) if mx else 0.0
    return ContextUsage(used_tokens=used, max_tokens=mx, percentage=pct, model=model)
