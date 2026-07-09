"""Tests for reading /context usage from a pi session transcript."""

from __future__ import annotations

import json

from patchbay.harness import (
    ContextQueryCapableHarness,
    PiHarness,
    UsageQueryCapableHarness,
)
from patchbay.harness.pi_session import (
    context_usage_from_session,
    find_session_file,
    session_usage,
)


def _write_session(path, lines):
    path.write_text("\n".join(json.dumps(o) for o in lines))


def test_pi_harness_is_context_query_capable():
    # The whole point of the per-harness pattern: PiHarness now satisfies the
    # capability protocol, so /context dispatches to it instead of refusing.
    assert isinstance(PiHarness(), ContextQueryCapableHarness)


def test_prefers_real_token_usage(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_session(f, [
        {"type": "session", "id": "abc", "cwd": "/x"},
        {"type": "message", "message": {"role": "user", "content": "hi"}},
        {"type": "message", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "hello"}],
            "usage": {"input": 1200, "cacheRead": 300, "cacheWrite": 0, "output": 50},
        }},
    ])
    cu = context_usage_from_session(f, "gpt-4o", fallback_window=10_000)
    # input + cacheRead + cacheWrite (not output)
    assert cu.used_tokens == 1500
    assert cu.percentage == round(1500 / cu.max_tokens * 100, 1)


def test_falls_back_to_estimate_when_usage_is_zero(tmp_path):
    # A local model that reports no tokens: estimate from transcript text.
    f = tmp_path / "s.jsonl"
    _write_session(f, [
        {"type": "message", "message": {"role": "user", "content": "x" * 400}},
        {"type": "message", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "y" * 400}],
            "usage": {"input": 0, "cacheRead": 0, "cacheWrite": 0, "output": 0},
        }},
    ])
    cu = context_usage_from_session(f, "nope/nope", fallback_window=10_000)
    assert cu.used_tokens > 0  # estimated from the ~800 chars of transcript


def test_pi_harness_is_usage_query_capable():
    assert isinstance(PiHarness(), UsageQueryCapableHarness)


def test_session_usage_sums_cost_and_tokens(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_session(f, [
        {"type": "message", "message": {"role": "user", "content": "hi"}},
        {"type": "message", "message": {
            "role": "assistant", "content": "a",
            "usage": {"input": 100, "output": 20, "totalTokens": 120, "cost": {"total": 0.0012}},
        }},
        {"type": "message", "message": {
            "role": "assistant", "content": "b",
            "usage": {"input": 200, "output": 30, "totalTokens": 230, "cost": {"total": 0.0034}},
        }},
    ])
    u = session_usage(f, model="gpt-4o")
    assert u.cost_usd == round(0.0012 + 0.0034, 4)
    assert u.input_tokens == 300
    assert u.output_tokens == 50
    assert u.total_tokens == 350
    assert u.model == "gpt-4o"


def test_find_session_file_matches_by_uuid(tmp_path):
    root = tmp_path / "sessions"
    cwd_dir = root / "--Users-me-Developer-proj--"
    cwd_dir.mkdir(parents=True)
    target = cwd_dir / "2026-07-08T00-00-00Z_019f-uuid-here.jsonl"
    target.write_text("{}")
    (cwd_dir / "2026-01-01T00-00-00Z_other-uuid.jsonl").write_text("{}")
    assert find_session_file(root, "019f-uuid-here") == target
    assert find_session_file(root, "no-such-uuid") is None
    assert find_session_file(root, None) is None
