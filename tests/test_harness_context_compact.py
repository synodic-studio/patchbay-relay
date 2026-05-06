"""Tests for the /context and /compact harness extensions.

Covers:
- ContextUsage / CompactResult dataclasses
- Capability-flag wiring across all harnesses
- ClaudeSdkHarness.get_context() against a stubbed SDK client
- ClaudeSdkHarness.compact() against a stubbed SDK client
- _fmt_k formatter
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from patchbay.harness import (
    CAPABILITIES_BY_NAME,
    ClaudeSdkHarness,
    CompactResult,
    ContextUsage,
    TurnRequest,
)
from patchbay.harness.claude_sdk import _fmt_k


def _req(tmp_path: Path, **kw) -> TurnRequest:
    defaults = dict(
        prompt="",
        session_key="t",
        project_dir=tmp_path,
        system_prompt="",
        resume_session_id=None,
        model=None,
        effort=None,
        allowed_tools=None,
        disallowed_tools=None,
        max_turns=None,
        plugin_dir=None,
    )
    defaults.update(kw)
    return TurnRequest(**defaults)


# ---- Dataclasses ----


def test_context_usage_has_required_fields():
    u = ContextUsage(used_tokens=100, max_tokens=1000, percentage=10.0)
    assert u.used_tokens == 100
    assert u.max_tokens == 1000
    assert u.percentage == 10.0
    assert u.model is None  # optional default


def test_compact_result_succeeded_minimal():
    r = CompactResult(succeeded=True, message="done")
    assert r.succeeded is True
    assert r.tokens_before is None
    assert r.tokens_after is None


# ---- Capabilities ----


def test_only_cc_sdk_supports_context_query_today():
    assert CAPABILITIES_BY_NAME["cc-sdk"].supports_context_query is True
    for name in ("cc-cli", "pi"):
        assert CAPABILITIES_BY_NAME[name].supports_context_query is False, (
            f"{name} should not advertise supports_context_query yet"
        )


def test_only_cc_sdk_supports_compact_today():
    assert CAPABILITIES_BY_NAME["cc-sdk"].supports_compact is True
    for name in ("cc-cli", "pi"):
        assert CAPABILITIES_BY_NAME[name].supports_compact is False


# ---- _fmt_k ----


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0"),
        (999, "999"),
        (1_000, "1.0k"),
        (29_937, "29.9k"),
        (1_000_000, "1.0M"),
        (1_500_000, "1.5M"),
    ],
)
def test_fmt_k(n, expected):
    assert _fmt_k(n) == expected


# ---- get_context() ----


class _FakeContextClient:
    """Stub ClaudeSDKClient for get_context tests."""

    def __init__(self, options=None):
        self.options = options
        self._usage_payloads = [
            {
                "totalTokens": 12345,
                "rawMaxTokens": 1_000_000,
                "maxTokens": 200_000,
                "percentage": 1.2,
                "model": "claude-opus-4-7[1m]",
            }
        ]
        self.queries: list[str] = []
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def __aenter__(self):
        self.connect_calls += 1
        return self

    async def __aexit__(self, *args):
        self.disconnect_calls += 1
        return False

    async def get_context_usage(self):
        return self._usage_payloads[-1]

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)
        # After /compact, simulate a smaller follow-up usage payload.
        if prompt.startswith("/compact"):
            self._usage_payloads.append(
                {
                    "totalTokens": 4321,
                    "rawMaxTokens": 1_000_000,
                    "maxTokens": 200_000,
                    "percentage": 0.4,
                }
            )

    async def receive_response(self):
        # Yield a single ResultMessage so the harness's drain loop exits.
        from claude_agent_sdk import ResultMessage  # may be a stub
        yield ResultMessage(
            session_id="ses-1",
            num_turns=1,
            total_cost_usd=0.0,
            result="",
            subtype="success",
            is_error=False,
            errors=[],
        )


@pytest.fixture
def fake_sdk(monkeypatch):
    """Inject a fake claude_agent_sdk module so the harness imports it."""
    module = types.ModuleType("claude_agent_sdk")

    class _Options:
        def __init__(self, **kw):
            self.kw = kw

    class _ResultMessage:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    module.ClaudeAgentOptions = _Options
    module.ClaudeSDKClient = _FakeContextClient
    module.ResultMessage = _ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return module


def test_get_context_returns_usage_dataclass(fake_sdk, tmp_path):
    h = ClaudeSdkHarness()

    async def go():
        return await h.get_context(_req(tmp_path))

    usage = asyncio.run(go())
    assert isinstance(usage, ContextUsage)
    assert usage.used_tokens == 12345
    assert usage.max_tokens == 1_000_000  # rawMaxTokens preferred over maxTokens
    assert usage.percentage == pytest.approx(1.2)
    assert usage.model == "claude-opus-4-7[1m]"


# ---- compact() ----


def test_compact_sends_slash_command_and_returns_before_after(fake_sdk, tmp_path):
    h = ClaudeSdkHarness()
    captured: dict = {}

    # Wrap factory so we can inspect the client after.
    original = fake_sdk.ClaudeSDKClient

    def _factory(options=None):
        client = original(options=options)
        captured["client"] = client
        return client

    fake_sdk.ClaudeSDKClient = _factory

    async def go():
        return await h.compact(_req(tmp_path), instructions=None)

    result = asyncio.run(go())
    assert result.succeeded is True
    assert result.tokens_before == 12345
    assert result.tokens_after == 4321
    assert captured["client"].queries == ["/compact"]
    assert "29.9k" not in result.message  # we used 12345 → "12.3k → 4.3k"
    assert "12.3k" in result.message and "4.3k" in result.message


def test_compact_passes_instructions_through(fake_sdk, tmp_path):
    h = ClaudeSdkHarness()
    captured: dict = {}
    original = fake_sdk.ClaudeSDKClient

    def _factory(options=None):
        client = original(options=options)
        captured["client"] = client
        return client

    fake_sdk.ClaudeSDKClient = _factory

    async def go():
        return await h.compact(_req(tmp_path), instructions="focus on the API design")

    result = asyncio.run(go())
    assert result.succeeded is True
    assert captured["client"].queries == ["/compact focus on the API design"]


def test_fallback_compact_handoff_prompt_contains_summary():
    """The handoff prompt that becomes the new session's first message
    must include the summary text and a brief framing."""
    import bridge as br

    out = br._build_handoff_prompt("Summary line 1\nSummary line 2")
    assert "Summary line 1" in out
    assert "Summary line 2" in out
    assert "carrying" in out.lower() or "carried" in out.lower()


def test_summarize_prompt_is_focused_and_steerable():
    """The synthesizer prompt should ask for ONLY the summary text and
    clearly define what to cover."""
    import bridge as br

    # Steering text must be appended verbatim so the summarizer respects it.
    base = br._SUMMARIZE_PROMPT
    assert "ONLY the summary" in base
    assert "decisions made" in base.lower() or "decisions" in base.lower()
    assert "work in progress" in base.lower() or "next" in base.lower()


def test_compact_surfaces_failure_as_compact_result(monkeypatch, tmp_path):
    """If the SDK call raises, compact() returns a failed CompactResult — never raises."""
    module = types.ModuleType("claude_agent_sdk")

    class _Options:
        def __init__(self, **kw):
            pass

    class _BadClient:
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_context_usage(self):
            raise RuntimeError("kaboom")

    module.ClaudeAgentOptions = _Options
    module.ClaudeSDKClient = _BadClient
    module.ResultMessage = type("ResultMessage", (), {})

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)

    h = ClaudeSdkHarness()

    async def go():
        return await h.compact(_req(tmp_path))

    result = asyncio.run(go())
    assert result.succeeded is False
    assert "kaboom" in result.message
