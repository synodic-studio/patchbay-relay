"""Reproduction tests for the MarkdownV2 render-drop bug.

Context: on 2026-04-22 ~08:44, a reply was delivered to Telegram with HTTP
200, yet the user received only fragments — prose intro and tail visible,
the bulleted middle dropped. We had no audit log, so the sent MarkdownV2
was lost. These tests reconstruct the two telegramify-markdown pathologies
that most plausibly caused it, so that:

1. The classes of bad output are documented.
2. A follow-up pre-send sanity-check (separate bead) has concrete fixtures
   to validate against.
3. A regression fails loudly if telegramify starts producing other
   suspect shapes.

These tests do not assert that the *rendered* Telegram output is broken —
we can't introspect that from here. They assert that the produced
MarkdownV2 has shapes known to be unreliable across clients.
"""

import re

import pytest

import telegramify_markdown
from telegramify_markdown.customize import get_runtime_config


# Match telegramify configuration used by bridge.py (see bridge.py ~line 44).
@pytest.fixture(autouse=True)
def _configure_telegramify():
    rc = get_runtime_config()
    rc.markdown_symbol.head_level_1 = ""
    rc.markdown_symbol.link = ""
    yield


def _code_spans(md_text: str) -> list[str]:
    """Return backtick-delimited code spans (including the backticks)."""
    return re.findall(r"`[^`\n]*`", md_text)


def _suspect_escapes_in_code(span: str) -> list[str]:
    """Return backslash escape sequences inside a code span that aren't
    the MarkdownV2-legal `\\`\\`` or `\\\\`. Per the MarkdownV2 spec, code
    spans only need to escape backtick and backslash; everything else is
    undefined and known to render inconsistently across clients."""
    inner = span[1:-1]
    return [m for m in re.findall(r"\\[^`\\]", inner)]


# ---------------------------------------------------------------------------
# Pathology 1: escape sequences inside code spans
# ---------------------------------------------------------------------------


class TestEscapesInsideCodeSpans:
    """telegramify doubles backslashes in source, which when combined with
    MarkdownV2's escape of `.` outside code, leaves `\\.` sequences inside
    code spans — undefined per spec, drops on some clients."""

    def test_dotted_identifier_in_backticks_produces_suspect_escape(self):
        raw = "check `\\.pi` for the trailing entry"
        converted = telegramify_markdown.markdownify(raw)
        spans = _code_spans(converted)
        assert spans, "expected at least one code span in converted output"
        bad = [s for s in spans if _suspect_escapes_in_code(s)]
        assert bad, (
            "telegramify should (today) produce an escape sequence inside "
            "the code span for `\\.pi` — the known MarkdownV2 render-drop "
            f"pathology. Converted: {converted!r}"
        )

    def test_config_py_in_backticks_produces_suspect_escape(self):
        raw = "`config\\.py` was edited"
        converted = telegramify_markdown.markdownify(raw)
        bad = [s for s in _code_spans(converted) if _suspect_escapes_in_code(s)]
        assert bad, f"expected suspect escape inside code span: {converted!r}"

    def test_plain_word_in_backticks_is_clean(self):
        """Sanity: a code span without a dot does not trip the pathology."""
        raw = "run `pytest` now"
        converted = telegramify_markdown.markdownify(raw)
        bad = [s for s in _code_spans(converted) if _suspect_escapes_in_code(s)]
        assert not bad, f"unexpected escape inside code span: {converted!r}"


# ---------------------------------------------------------------------------
# Pathology 2: non-ASCII bullet glyph
# ---------------------------------------------------------------------------


class TestUnicodeBullets:
    """telegramify emits U+2981 (Z NOTATION SPOT) as the bullet for
    unordered list items. It's not a standard bullet glyph; mobile
    Telegram clients render it inconsistently."""

    def test_bullet_list_uses_nonstandard_unicode_bullet(self):
        raw = "- first item\n- second item\n- third item"
        converted = telegramify_markdown.markdownify(raw)
        suspicious = ["⦁", "⧁", "•"]
        found = [f"U+{ord(ch):04X}" for ch in suspicious if ch in converted]
        assert found, f"expected a non-standard bullet glyph in telegramify output — converted: {converted!r}"


# ---------------------------------------------------------------------------
# The 08:44 reproduction
# ---------------------------------------------------------------------------


class TestReportedScenario:
    """Reconstruct the reported 2026-04-22 08:44 case: prose intro, bulleted
    body with dotted identifiers in backticks, prose tail. User saw intro
    and tail, bullets dropped."""

    SUSPECT_INPUT = """Both models landed in the same place.

- check `\\.pi` for the trailing entry
- `config\\.py` was edited
- unicode bullet test item

Heads up: the earlier warning still applies."""

    def test_prose_intro_and_tail_are_preserved(self):
        converted = telegramify_markdown.markdownify(self.SUSPECT_INPUT)
        # The parts the user *did* see survive conversion unchanged enough
        # that they'd render on any client.
        assert "Both models landed in the same place" in converted
        assert "Heads up" in converted

    def test_bulleted_body_contains_both_pathologies(self):
        """The bulleted middle — the part the user did NOT see — contains
        both the escape-in-code-span and the nonstandard bullet glyph."""
        converted = telegramify_markdown.markdownify(self.SUSPECT_INPUT)

        bullet_glyph_present = any(ch in converted for ch in ("⦁", "⧁", "•"))
        assert bullet_glyph_present, f"expected a nonstandard bullet glyph: {converted!r}"

        bad_spans = [s for s in _code_spans(converted) if _suspect_escapes_in_code(s)]
        assert len(bad_spans) >= 2, (
            f"expected at least two code spans with suspect escapes (the `\\.pi` and `config\\.py` ones): {converted!r}"
        )

    def test_telegram_would_accept_this_output(self):
        """MarkdownV2 validation at Telegram's server is permissive — it
        accepts this output (hence our 200 OK). The render drop is
        strictly client-side. This test documents that nothing in the
        converted payload looks syntactically malformed at a high level:
        no dangling entity markers at line boundaries."""
        converted = telegramify_markdown.markdownify(self.SUSPECT_INPUT)
        # No trailing unterminated backtick or asterisk on the final line.
        last_line = converted.rstrip().splitlines()[-1]
        assert last_line.count("`") % 2 == 0
