"""Quota / rate-limit detection."""

from .parser import _extract_text_from_events

# Patterns confirmed from Claude CLI source:
#   stderr: "Please wait and try again later"
#   API error type: "rate_limit_error" (429), "overloaded_error" (529)
_QUOTA_PATTERNS_STDERR = [
    "please wait and try again later",
    "rate limit",
    "rate_limit",
]
_QUOTA_PATTERNS_ERROR = [
    "rate_limit_error",
    "overloaded_error",
    "rate limit",
    "rate limited",
    "too many requests",
    "usage limit",
]


def is_quota_error(events: list[dict], stderr: str) -> bool:
    """Detect whether a Claude invocation failed due to quota or rate limiting."""
    haystack = stderr.lower()
    if any(p in haystack for p in _QUOTA_PATTERNS_STDERR):
        return True
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result:
        error = str(result.get("error", "")).lower()
        if any(p in error for p in _QUOTA_PATTERNS_ERROR):
            return True
    text = _extract_text_from_events(events)
    if text and len(text) < 300:
        text_lower = text.lower()
        if any(p in text_lower for p in _QUOTA_PATTERNS_ERROR):
            return True
    return False
