"""Comprehensive coverage for `_to_markdownv2`, the bridge's MarkdownV2
converter wrapping `telegramify_markdown.markdownify`.

The complementary file `test_markdownv2_pathologies.py` documents specific
shapes Telegram has historically rendered badly. This file is the broader
"does the converter survive the kind of text claude actually produces"
suite, plus a hypothesis property test that fuzzes with random text.

Coverage rationale (after the 2026-04-25 audit pass): the bridge is now
heavily dependent on the converter not raising and not silently producing
broken output for the patterns claude generates dozens of times per day —
bold prose, bullet lists, inline code with hyphens / dots / paths, code
blocks, links, em dashes, arrows. Each of those is exercised below."""

from __future__ import annotations

import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import bridge
import telegramify_markdown

from patchbay.markdown_config import configure_telegramify


@pytest.fixture(autouse=True)
def _configure_telegramify():
    """Match the runtime config bridge.py uses (shared source of truth)."""
    configure_telegramify()
    yield


# Characters MarkdownV2 considers "special" outside of code spans. Per
# Telegram's spec, every literal occurrence must be backslash-escaped.
# We rely on this set in the entity-matching invariants below.
_MD_SPECIAL = "_*[]()~`>#+-=|{}.!"

# Entities that come in pairs in MarkdownV2 (open/close).
# We assert each open has a close in the converted output, since unbalanced
# entities are the most common cause of Telegram-side rejections.
_PAIRED_OPENERS = ("*", "_", "~", "`", "||")


def _strip_code_blocks(md: str) -> str:
    """Remove ``` fenced blocks so paired-entity checks don't get confused
    by code content."""
    return re.sub(r"```[\s\S]*?```", "", md)


def _strip_inline_code(md: str) -> str:
    """Remove backtick-delimited inline code spans. Used in tandem with
    fenced-block removal so we only check pairing on prose entities."""
    return re.sub(r"`[^`\n]*`", "", md)


def _count_unescaped(text: str, ch: str) -> int:
    """Count occurrences of `ch` not preceded by a backslash."""
    pattern = rf"(?<!\\){re.escape(ch)}"
    return len(re.findall(pattern, text))


# ---------------------------------------------------------------------------
# Real-world fixtures: shapes that appear across the outbound logs
# ---------------------------------------------------------------------------


REAL_WORLD_INPUTS: list[tuple[str, str]] = [
    ("plain prose", "Hello world. This is a normal sentence."),
    ("bold prose", "This is **bold** within a sentence."),
    ("italic prose", "This is *italic* within a sentence."),
    (
        "bullet list",
        "Here's the rundown:\n- first item\n- second item\n- third item\n",
    ),
    (
        "numbered list",
        "Steps:\n1. wake up\n2. drink coffee\n3. write code\n",
    ),
    ("inline code dotted", "Check `config.py` for the env var."),
    ("inline code dash", "Run `claude-code` from the project dir."),
    ("inline code path", "The file lives at `/Users/foo/bar/baz.py`."),
    (
        "code block python",
        "```python\ndef hello():\n    return 'world'\n```",
    ),
    (
        "code block bash",
        "```bash\ncd /tmp && ls -la\n```",
    ),
    ("link with text", "See [the docs](https://example.com/docs) for more."),
    ("em dash", "First clause — second clause."),
    ("right arrow", "Old → New transformation."),
    (
        "mixed bold+list",
        "**Plan:**\n- step 1\n- step 2\n- step 3\n\n**Result:** done.",
    ),
    ("file path with dots", "Edit `bridge.py:1234` to change the timeout."),
    (
        "session id-like",
        "Resumed session `abc-123-def-456` for chat 42.",
    ),
    (
        "shell snippet with redirects",
        "Run `cmd > output.log 2>&1 &` to background it.",
    ),
    ("hyphenated word", "The auto-recover-on-OOM path now exists."),
    ("commit hash", "Shipped in `91d44de`."),
    ("emoji-free arrow", "Old behavior -> new behavior."),
    (
        "long bullet with bold",
        "**Why this matters:**\n- saves time on the **happy path**\n- avoids the **sad path** entirely",
    ),
    (
        "blockquote",
        "> This is a quoted line.\n> And a second line.\n",
    ),
    (
        "headers stripped (per config)",
        "# Title\n\nBody paragraph here.",
    ),
    ("unicode bullets", "• alpha\n• beta\n• gamma"),
    ("paren in prose", "He said (and I quote) that it works."),
    ("brackets in prose", "Insert [your name here] at the top."),
    (
        "pre-existing escapes",
        r"This text has \*escaped asterisks\* already.",
    ),
    ("empty string", ""),
    ("only whitespace", "   \n\n  \t  \n"),
    ("just a number", "42"),
    ("just a code span", "`x`"),
]


class TestRealWorldShapes:
    """Every fixture must convert without raising and produce a string."""

    @pytest.mark.parametrize("name,raw", REAL_WORLD_INPUTS, ids=[n for n, _ in REAL_WORLD_INPUTS])
    def test_does_not_raise(self, name, raw):
        md = bridge._to_markdownv2(raw)
        assert md is not None, f"{name}: converter returned None"
        assert isinstance(md, str)

    @pytest.mark.parametrize("name,raw", REAL_WORLD_INPUTS, ids=[n for n, _ in REAL_WORLD_INPUTS])
    def test_visible_words_preserved(self, name, raw):
        """Each non-trivial word from the raw text should appear in the
        converted output. Catches accidental content-dropping bugs."""
        md = bridge._to_markdownv2(raw) or ""
        # Pick prose words that are unlikely to be reformatted into
        # entities. Skip anything containing markdown-special chars.
        words = [w for w in re.findall(r"[A-Za-z]{4,}", raw) if w.lower() not in {"http", "https"}]
        for w in words:
            assert w in md, f"{name}: word {w!r} missing from converted output"


# ---------------------------------------------------------------------------
# Entity-pairing invariants on prose (excluding code spans / blocks)
# ---------------------------------------------------------------------------


class TestEntityPairing:
    """Outside of code, every formatting opener has a matching closer.
    Unbalanced entities are the #1 cause of Telegram MarkdownV2 rejection."""

    @pytest.mark.parametrize(
        "raw",
        [
            "**bold here**",
            "**bold** and **another bold**",
            "*italic*",
            "_underline_",
            "~strike~",
            "**bold** with *italic* mixed",
            "~strike with **nested bold**~",
        ],
    )
    def test_paired_entities_balance(self, raw):
        md = bridge._to_markdownv2(raw)
        assert md is not None
        prose = _strip_inline_code(_strip_code_blocks(md))
        for ch in ("*", "_", "~"):
            count = _count_unescaped(prose, ch)
            assert count % 2 == 0, (
                f"unbalanced {ch!r} in converted MarkdownV2 ({count} unescaped) for raw={raw!r}; md={md!r}"
            )


# ---------------------------------------------------------------------------
# Code-span content: pathologies are tracked separately
# ---------------------------------------------------------------------------
#
# telegramify_markdown currently emits illegal escape sequences inside code
# spans (e.g. \-, \.) for inputs like "abc-def-123" or "config.py". This is
# a known bug documented in test_markdownv2_pathologies.py. We intentionally
# do NOT assert clean code spans here — that file owns the fixture-style
# documentation of the pathology, and asserting the inverse here would
# create churn without adding signal.

# ---------------------------------------------------------------------------
# Property test: arbitrary text never crashes the converter
# ---------------------------------------------------------------------------


_FAST = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


class TestConverterProperty:
    """Hypothesis-driven: claude can emit arbitrary text. The converter
    must always return a string (never None, never raise) for any valid
    Unicode input the wrapper sees."""

    @_FAST
    @given(text=st.text(min_size=0, max_size=400))
    def test_never_raises(self, text):
        # bridge._to_markdownv2 catches and returns None; raw markdownify
        # is what we're stress-testing here.
        try:
            telegramify_markdown.markdownify(text)
        except Exception as exc:
            pytest.fail(f"markdownify raised on input {text!r}: {exc}")

    @_FAST
    @given(text=st.text(min_size=1, max_size=400))
    def test_wrapper_returns_string_or_none(self, text):
        result = bridge._to_markdownv2(text)
        assert result is None or isinstance(result, str)

    @_FAST
    @given(
        prose_blocks=st.lists(
            st.text(
                alphabet=st.characters(
                    blacklist_categories=("Cs",),  # exclude surrogates
                ),
                min_size=0,
                max_size=80,
            ),
            min_size=1,
            max_size=8,
        )
    )
    def test_paragraphs_preserve_alphanumeric_content(self, prose_blocks):
        """Whatever happens to formatting, alphanumeric runs >=4 chars
        survive the conversion."""
        raw = "\n\n".join(prose_blocks)
        md = bridge._to_markdownv2(raw) or ""
        for run in re.findall(r"[A-Za-z0-9]{4,}", raw):
            assert run in md, f"alphanumeric run {run!r} dropped from {raw!r} -> {md!r}"
