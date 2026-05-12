"""Single source of truth for telegramify-markdown runtime config.

The bridge and the test suite both call `configure_telegramify()` so the
converted MarkdownV2 output is identical in tests and production. Before
this was centralized, the test fixtures set `head_level_1 = ""` while
production set `head_level_1 = ">"`, and the resulting drift hid a real
parsing bug for headings (`>` is a MarkdownV2 special character — using
it as a heading prefix produced `*> heading*`, which Telegram parses as
"open bold + start blockquote inside bold" and rejects with
`can't find end of bold entity`).

Heading prefixes use `▸` (U+25B8) because it is not a MarkdownV2 special
character and gives visual hierarchy via repetition (`▸`, `▸▸`, `▸▸▸`).
"""

from __future__ import annotations

from telegramify_markdown.customize import get_runtime_config

HEAD_LEVEL_1 = "▸"
HEAD_LEVEL_2 = "▸▸"
HEAD_LEVEL_3 = "▸▸▸"
HEAD_LEVEL_4 = "▸▸▸"


def configure_telegramify() -> None:
    """Apply the bridge's standard telegramify-markdown runtime config."""
    cfg = get_runtime_config()
    sym = cfg.markdown_symbol
    sym.head_level_1 = HEAD_LEVEL_1
    sym.head_level_2 = HEAD_LEVEL_2
    sym.head_level_3 = HEAD_LEVEL_3
    sym.head_level_4 = HEAD_LEVEL_4
    sym.image = ""
    sym.link = ""
