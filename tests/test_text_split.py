"""Tests for patchbay.text_split.

Covers the splitter's preference for paragraph > line > word boundaries,
the MarkdownV2 parity check, the paired raw/md splitter, and the
GravityWell regression — a long markdown response whose naive byte-slice
chunking produced an orphan `*` in chunk 1 and made Telegram return
`BadRequest: can't find end of bold entity at byte offset 319`.
"""

from __future__ import annotations

import telegramify_markdown

from patchbay.text_split import (
    is_markdownv2_balanced,
    split_for_telegram,
    split_paired_for_telegram,
)


class TestSplitForTelegram:
    def test_empty_returns_empty_list(self):
        assert split_for_telegram("", 100) == []

    def test_under_limit_returns_single_chunk(self):
        assert split_for_telegram("hello world", 100) == ["hello world"]

    def test_exactly_at_limit_returns_single_chunk(self):
        text = "a" * 10
        assert split_for_telegram(text, 10) == [text]

    def test_prefers_paragraph_break(self):
        text = "alpha bravo\n\ncharlie delta echo"
        chunks = split_for_telegram(text, 20)
        assert chunks == ["alpha bravo", "charlie delta echo"]

    def test_prefers_line_break_when_no_paragraph(self):
        text = "alpha bravo\ncharlie delta echo"
        chunks = split_for_telegram(text, 20)
        assert chunks == ["alpha bravo", "charlie delta echo"]

    def test_prefers_word_break_when_no_line(self):
        text = "alpha bravo charlie delta"
        chunks = split_for_telegram(text, 15)
        assert chunks == ["alpha bravo", "charlie delta"]

    def test_hard_cuts_when_no_boundary_in_window(self):
        text = "a" * 25
        chunks = split_for_telegram(text, 10)
        assert chunks == ["a" * 10, "a" * 10, "a" * 5]

    def test_no_chunk_starts_with_separator(self):
        text = "a" * 50 + "\n\n" + "b" * 50
        chunks = split_for_telegram(text, 60)
        for c in chunks:
            assert not c.startswith("\n")
            assert not c.startswith(" ")

    def test_long_text_with_mixed_boundaries(self):
        # Three paragraphs that exceed the limit when packed together;
        # paragraph boundaries dominate.
        para = "x" * 30
        text = "\n\n".join([para, para, para])
        chunks = split_for_telegram(text, 50)
        assert chunks == [para, para, para]


class TestIsMarkdownV2Balanced:
    def test_empty_text_is_balanced(self):
        assert is_markdownv2_balanced("") is True

    def test_no_delimiters_is_balanced(self):
        assert is_markdownv2_balanced("plain ascii text") is True

    def test_balanced_bold(self):
        assert is_markdownv2_balanced("*hello*") is True

    def test_unbalanced_bold(self):
        assert is_markdownv2_balanced("hello *world") is False

    def test_balanced_italic(self):
        assert is_markdownv2_balanced("_hello_") is True

    def test_balanced_underline_distinct_from_italic(self):
        # `__x__` is two underline markers, not four italic markers.
        assert is_markdownv2_balanced("__hello__") is True

    def test_balanced_strikethrough(self):
        assert is_markdownv2_balanced("~hello~") is True

    def test_balanced_spoiler(self):
        assert is_markdownv2_balanced("||hello||") is True

    def test_balanced_inline_code(self):
        assert is_markdownv2_balanced("`code`") is True

    def test_balanced_code_block(self):
        assert is_markdownv2_balanced("```\nfoo\n```") is True

    def test_unbalanced_code_block(self):
        assert is_markdownv2_balanced("```unclosed") is False

    def test_escaped_delimiters_dont_count(self):
        # `\*` should not count as a bold marker.
        assert is_markdownv2_balanced("hello \\*world") is True

    def test_escaped_then_real_delimiter(self):
        # One escape + one real → odd real count → unbalanced.
        assert is_markdownv2_balanced("\\* and *") is False

    def test_mixed_balanced(self):
        assert is_markdownv2_balanced("*bold* and _italic_ and `code`") is True


class TestSplitPairedForTelegram:
    def test_empty_inputs_return_empty(self):
        assert split_paired_for_telegram("", "", 100) == []

    def test_short_paired_text_returns_single_pair(self):
        pairs = split_paired_for_telegram("raw", "md", 100)
        assert pairs == [("raw", "md")]

    def test_paragraph_alignment(self):
        raw = "first paragraph\n\nsecond paragraph\n\nthird paragraph"
        md = "first md\n\nsecond md\n\nthird md"
        pairs = split_paired_for_telegram(raw, md, 20)
        # Each paragraph fits alone but not together → one pair per paragraph.
        assert pairs == [
            ("first paragraph", "first md"),
            ("second paragraph", "second md"),
            ("third paragraph", "third md"),
        ]

    def test_packs_multiple_paragraphs_when_they_fit(self):
        raw = "a\n\nb\n\nc"
        md = "A\n\nB\n\nC"
        pairs = split_paired_for_telegram(raw, md, 100)
        assert pairs == [("a\n\nb\n\nc", "A\n\nB\n\nC")]


class TestGravityWellRegression:
    """The actual failure logged on 2026-05-02: telegramify converts the
    Library agent's project list, the naive splitter cut chunk 1 inside a
    bold marker, Telegram rejected with offset-319 bold-entity error and
    the message vanished. The new splitter must produce only balanced
    chunks for this input.
    """

    def _build_input(self) -> str:
        # Roughly the structure that triggered the original failure: many
        # `**Field:**` paragraphs and a `## Heading` between them, long
        # enough to require multi-chunk splitting.
        sections = []
        for name in ("CafeTuner", "GravityWell", "Patchbay", "Forge"):
            sections.append(
                f"## {name}\n\n"
                f"**Description:** A long description of {name} that goes on "
                f"for several sentences to inflate the size of this paragraph "
                f"so that splitting actually has to choose a boundary. "
                f"It mentions things like cameras, balls, and orbits.\n"
                f"**Status:** In Progress (deployed, supporting installs)\n"
                f"**My Role:** Sole engineer, ongoing support\n"
                f"**Key Collaborators:** Dimitri Klebe (primary partner, "
                f"NSSTI), Eddie Goldstein (initial backer, unidirectional)\n"
                f"**What Done Looks Like:** Long-term install stability "
                f"across venues; possible commercial evolution"
            )
        # Add enough repetition to push past a single 4096-char chunk.
        return "\n\n".join(sections * 3)

    def test_converted_chunks_are_all_balanced(self):
        raw = self._build_input()
        md = telegramify_markdown.markdownify(raw)
        # Sanity: input is large enough to require multi-chunk split.
        assert len(md) > 4096
        pairs = split_paired_for_telegram(raw, md, 4096)
        assert len(pairs) >= 2, "expected multi-chunk split"
        for _, md_chunk in pairs:
            assert is_markdownv2_balanced(md_chunk), (
                f"unbalanced chunk would trigger Telegram rejection:\n"
                f"{md_chunk[:200]}…"
            )

    def test_minimal_orphan_bold_chunk_caught_by_parity(self):
        """Direct check: if any chunk ends up unbalanced by any code path,
        the parity check flags it. This is the invariant that protects
        the user from Telegram's bold-entity rejection."""
        # Two-chunk converted output where chunk 0 closes its bold and
        # chunk 1 contains an orphan `*` (the shape that produced the
        # 2026-05-02 failure).
        chunk_a = "*Title:* content here that stays balanced"
        chunk_b = "more content with an orphan *opener and no closer"
        assert is_markdownv2_balanced(chunk_a) is True
        assert is_markdownv2_balanced(chunk_b) is False
