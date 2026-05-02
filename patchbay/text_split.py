"""Boundary-aware text splitting for Telegram messages.

The previous chunker byte-sliced the raw markdown response and converted
each slice independently. When a slice fell inside a markdown construct
(`**bold**`, a heading, etc.) `telegramify_markdown` produced unbalanced
MarkdownV2 (orphan `*`, mangled blockquote arrows). Telegram rejected the
chunk with `BadRequest: can't find end of bold entity at byte offset N`
and the user saw nothing.

The replacement: convert the whole response once, then split the
already-converted MarkdownV2 text on paragraph (`\\n\\n`) > line (`\\n`) >
word boundaries, never mid-character. We also expose a parity check the
caller uses to verify each chunk has balanced toggle entities; if any
chunk fails parity, the caller downgrades the entire response to plain
text rather than ship a half-formatted message.

Plain-text responses use the same splitter for consistent UX (no more
mid-word cuts when paragraph or line breaks are available).
"""

from __future__ import annotations

# MarkdownV2 toggle entities. Order matters: longer delimiters are matched
# first so `__` (underline) doesn't count as two `_` (italic), and ``` (code
# block) doesn't count as three ` (inline code). `**` is included for
# defensive parity even though telegramify normalizes bold to `*`; if a stray
# `**` slips through it should still parse evenly.
_DELIMS_LONGEST_FIRST: tuple[str, ...] = ("```", "**", "__", "||", "*", "_", "~", "`")


def split_for_telegram(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces no longer than ``limit`` characters.

    Prefers the latest paragraph break within the limit, then the latest
    line break, then the latest space. Falls back to a hard cut at
    ``limit`` only if no boundary exists in the leading window (e.g. a
    pathological 5000-character single token). The chosen separator is
    consumed so chunks don't start with stray `\\n` or spaces.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut, sep_len = _best_cut(remaining, limit)
        pieces.append(remaining[:cut])
        remaining = remaining[cut + sep_len :]
    if remaining:
        pieces.append(remaining)
    return pieces


def _best_cut(text: str, limit: int) -> tuple[int, int]:
    """Return ``(cut_index, separator_length)`` for the safest split ≤ limit."""
    paragraph = text.rfind("\n\n", 0, limit)
    if paragraph > 0:
        return paragraph, 2
    line = text.rfind("\n", 0, limit)
    if line > 0:
        return line, 1
    word = text.rfind(" ", 0, limit)
    if word > 0:
        return word, 1
    return limit, 0


def split_paired_for_telegram(
    raw: str, md: str, limit: int
) -> list[tuple[str, str]]:
    """Split paired raw and converted MarkdownV2 text into aligned chunks.

    Telegramify-markdown preserves paragraph (`\\n\\n`) boundaries during
    conversion, so pairing raw and md by paragraph index keeps the audit
    log's ``raw`` field meaningful and lets a per-chunk MarkdownV2 send
    failure cleanly downgrade to a sensible plain chunk.

    Greedy packing: each paragraph pair is appended to the current chunk
    if both raw and md still fit within ``limit``; otherwise a new chunk
    starts. A single paragraph that is itself oversize is split with the
    standalone splitter independently for raw and md and the resulting
    pieces zipped (with empty padding if counts diverge).

    If raw and md have different paragraph counts (unusual — would mean
    conversion lost or duplicated a `\\n\\n`), falls back to independent
    splits zipped with empty padding.
    """
    if not raw and not md:
        return []
    raw_paras = raw.split("\n\n")
    md_paras = md.split("\n\n")
    if len(raw_paras) != len(md_paras):
        return _zip_pad(
            split_for_telegram(raw, limit),
            split_for_telegram(md, limit),
        )

    pairs: list[tuple[str, str]] = []
    cur_raw: list[str] = []
    cur_md: list[str] = []
    cur_raw_len = 0
    cur_md_len = 0

    for r, m in zip(raw_paras, md_paras):
        sep_len = 2 if cur_raw else 0
        next_raw_len = cur_raw_len + sep_len + len(r)
        next_md_len = cur_md_len + sep_len + len(m)
        if next_raw_len <= limit and next_md_len <= limit:
            if cur_raw:
                cur_raw.append("\n\n")
                cur_md.append("\n\n")
            cur_raw.append(r)
            cur_md.append(m)
            cur_raw_len = next_raw_len
            cur_md_len = next_md_len
            continue
        if cur_raw:
            pairs.append(("".join(cur_raw), "".join(cur_md)))
            cur_raw, cur_md = [], []
            cur_raw_len = cur_md_len = 0
        if len(r) <= limit and len(m) <= limit:
            cur_raw.append(r)
            cur_md.append(m)
            cur_raw_len = len(r)
            cur_md_len = len(m)
        else:
            pairs.extend(
                _zip_pad(
                    split_for_telegram(r, limit),
                    split_for_telegram(m, limit),
                )
            )
    if cur_raw:
        pairs.append(("".join(cur_raw), "".join(cur_md)))
    return pairs


def _zip_pad(a: list[str], b: list[str]) -> list[tuple[str, str]]:
    """Zip two lists, padding the shorter with empty strings."""
    n = max(len(a), len(b))
    a = a + [""] * (n - len(a))
    b = b + [""] * (n - len(b))
    return list(zip(a, b))


def is_markdownv2_balanced(text: str) -> bool:
    """Return True iff every MarkdownV2 toggle entity occurs an even number of times.

    Backslash-escaped characters (``\\*``, ``\\_``, etc.) are skipped so they
    do not count toward delimiter parity. Greedy matches longest-first so
    overlapping delimiters (``__`` vs ``_``) are counted correctly.
    """
    counts: dict[str, int] = dict.fromkeys(_DELIMS_LONGEST_FIRST, 0)
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        for delim in _DELIMS_LONGEST_FIRST:
            if text.startswith(delim, i):
                counts[delim] += 1
                i += len(delim)
                break
        else:
            i += 1
    return all(c % 2 == 0 for c in counts.values())
