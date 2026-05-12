"""Regression: headings must not produce a `*>` pattern.

The bridge used to configure telegramify-markdown with `>` as the heading
prefix (so `# Heading` rendered as `*> Heading*`). `>` is a MarkdownV2
special character (blockquote opener), so `*>` is parsed by Telegram as
"open bold, then start blockquote inside bold" — invalid, and the send
fails with `Can't parse entities: can't find end of bold entity at byte
offset N`.

`activity.jsonl` recorded 17 production failures with this exact
signature before the fix (heading prefixes changed to `▸`/`▸▸`/`▸▸▸`,
which are not MarkdownV2 special characters). The raw inputs from those
failures are embedded below as fixtures so this regression cannot return.
"""

from __future__ import annotations

import re

import pytest
import telegramify_markdown

from patchbay.markdown_config import configure_telegramify


@pytest.fixture(autouse=True)
def _configure_telegramify():
    configure_telegramify()
    yield


# Raw inputs from the 17 historical Telegram-rejected sends in
# `activity.jsonl` (event `markdown_send_failed`, error
# `can't find end of bold entity`). Truncated to the heading-bearing
# prefix; the bug reproduces from the first heading.
HISTORICAL_FAILURES: list[str] = [
    "# Stargate Harness Migration Plan\n\n## Goal\nReplace the `claude` CLI subprocess with the Claude Code SDK.",
    '# Section 1 — Frontmatter + Intro (lines 1–17)\n\n```\ntitle: "Telegram as a Development Interface"\n```',
    "# Section 2 — The Constraint (lines 19–31)\n\n> ## The Constraint: No GUI, No Browser, No Terminal\n>\n> The premise is simple.",
    "Section 2 polished.\n\n> ## The Constraint: No GUI, No Browser, No Terminal\n>\n> If a development task requires opening a browser.",
    "Section 3 fixed.\n\n# Section 4 — Patchbay Relay Architecture (lines 48–66)\n\n> ## Patchbay Relay Architecture\n>\n> Patchbay Relay is a Python application.",
    "# Section 5 — Session Management and Crash Recovery (lines 68–77)\n\n> ### Session Management and Crash Recovery\n>\n> The bridge runs as a launchd service.",
    '# Section 6 — Multi-Project Routing (lines 79–103)\n\n> ### Multi-Project Routing\n>\n> `chat_projects.json` maps each Telegram topic to a project directory.',
    "# Section 6 — Multi-Project Routing\n\n> ### Multi-Project Routing\n>\n> `chat_projects.json` maps each Telegram topic.",
    "# Section 7 — The go.kj6.dev URL Wrapper (lines 107–122)\n\n> ## The go.kj6.dev URL Wrapper\n>\n> Telegram does not support custom URL schemes.",
    "Shipped. Worker code on stargate develop.\n\n# Section 7 — Tappable Links to Native Apps",
    'Four more sections.\n\n# Section 8 — What "Headless-First" Development Looks Like (lines 122–146)\n\n> ## What "Headless-First" Development Looks Like\n>\n> A typical development session.',
    "Section 8 deleted.\n\n# Section 9 — TCC Monitoring (now lines 122–130)\n\n> ## TCC Monitoring: The Invisible Failure Mode\n>\n> macOS TCC permissions are binary-path-specific.",
    'Bead created: **Fanta-gy5**.\n\n# Section 9 — Pinning Python to Avoid TCC Outages',
    "**Re: TCC via CLI** — short answer: no, not really.\n\na. `tccutil` only supports `reset`, never `grant`.",
    '# Section 10 — The Matrix Bridge Alternative (lines 134–145)\n\nReal flag before I edit: the post says **"Patchbay Relay also includes a Matrix bridge"**.',
    "Section 10 deleted.\n\n> Matrix was the runner-up. I prototyped a separate bridge for it.",
    "**Key Collaborators:** Solo\n**What Done Looks Like:** Sustainable income from shipped products.\n\n## GravityWell\n\n**Description:** Science-exhibit macOS app.",
]


# `*>` (bold-open immediately followed by blockquote-open) is the Telegram
# parser failure signature. `**>` is a different construct (expandable
# blockquote in some MarkdownV2 dialects) and not what tripped us, so it
# must not match.
_BAD_BOLD_BLOCKQUOTE = re.compile(r"(?<!\\)(?<!\*)\*>(?!\*)")


@pytest.mark.parametrize("raw", HISTORICAL_FAILURES, ids=range(len(HISTORICAL_FAILURES)))
def test_historical_failure_no_longer_produces_bold_blockquote(raw: str) -> None:
    """Each historical-failure raw input must convert to MarkdownV2 that
    contains no `*>` (bold-open + blockquote-open) sequence."""
    md = telegramify_markdown.markdownify(raw)
    bad_matches = _BAD_BOLD_BLOCKQUOTE.findall(md)
    assert not bad_matches, (
        f"Heading conversion regressed: produced `*>` (bold containing "
        f"blockquote opener). Output: {md!r}"
    )


def test_heading_prefix_is_not_a_markdownv2_special_char() -> None:
    """Defensive: if someone changes the heading prefix back to a MarkdownV2
    special character, fail loud rather than waiting for a Telegram reject."""
    from patchbay.markdown_config import (
        HEAD_LEVEL_1,
        HEAD_LEVEL_2,
        HEAD_LEVEL_3,
        HEAD_LEVEL_4,
    )

    md_special = set("_*[]()~`>#+-=|{}.!")
    for level, prefix in enumerate(
        [HEAD_LEVEL_1, HEAD_LEVEL_2, HEAD_LEVEL_3, HEAD_LEVEL_4], start=1
    ):
        special_chars_in_prefix = set(prefix) & md_special
        assert not special_chars_in_prefix, (
            f"head_level_{level} = {prefix!r} contains MarkdownV2 special "
            f"chars {special_chars_in_prefix} — will collide with bold/quote "
            f"parsing and cause Telegram entity rejections"
        )


def test_plain_heading_converts_to_balanced_bold() -> None:
    """A simple heading must produce a balanced bold construct with no
    embedded special characters that could open new entities."""
    md = telegramify_markdown.markdownify("# Plain heading")
    # exactly two unescaped asterisks (one opener, one closer)
    unescaped_stars = len(re.findall(r"(?<!\\)\*", md))
    assert unescaped_stars == 2, f"expected 2 unescaped `*`, got {unescaped_stars} in {md!r}"
    assert not _BAD_BOLD_BLOCKQUOTE.search(md), f"`*>` pattern present in {md!r}"


def test_blockquoted_heading_does_not_produce_bold_blockquote() -> None:
    """The combo `> ## Heading` is the most common real-world failure shape
    in the activity log. It must not produce `*>` after conversion."""
    md = telegramify_markdown.markdownify(
        "> ## Heading inside quote\n> body line\n> more body"
    )
    assert not _BAD_BOLD_BLOCKQUOTE.search(md), f"`*>` pattern present in {md!r}"
