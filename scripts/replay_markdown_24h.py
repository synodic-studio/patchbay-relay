"""Replay all claude-response audit entries from the last 24h through
_to_markdownv2 and report any that fail conversion or look suspicious.

This is a diagnostic script, not a test — it reads the bridge's outbound
audit log to find real responses we just sent, then runs each through
the current converter to surface regressions before they bite users.

Usage:
    uv run python scripts/replay_markdown_24h.py            # just summary
    uv run python scripts/replay_markdown_24h.py --verbose  # print first
                                                            # 200 chars of
                                                            # each failing
                                                            # raw text
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import telegramify_markdown

OUTBOUND_DIR = Path(__file__).resolve().parent.parent / "outbound"
DEFAULT_WINDOW_SEC = 24 * 3600


def _scan(window_sec: float, verbose: bool) -> int:
    cutoff = time.time() - window_sec
    total = 0
    failures: list[tuple[str, str, str]] = []  # (file, error, raw)
    suspicious_round_trip: list[tuple[str, str]] = []  # (file, raw) — converts but produces
    # leftover \* / \_ characters that suggest the raw input had unbalanced
    # entities; the converted output may render badly even if Telegram accepts it.

    for log_file in sorted(OUTBOUND_DIR.glob("*.jsonl")):
        try:
            for line in log_file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("source") != "claude-response":
                    continue
                if entry.get("ts", 0) < cutoff:
                    continue
                raw = entry.get("raw_text") or ""
                if not raw:
                    continue
                total += 1
                try:
                    md = telegramify_markdown.markdownify(raw)
                except Exception as exc:
                    failures.append((log_file.name, f"{type(exc).__name__}: {exc}", raw))
                    continue
                # Heuristic: paired escapes that the converter left dangling
                # because the raw input itself was malformed.
                if md.count(r"\*") % 2 != 0 or md.count(r"\_") % 2 != 0:
                    suspicious_round_trip.append((log_file.name, raw))
        except OSError as exc:
            print(f"  ! could not read {log_file}: {exc}", file=sys.stderr)

    print(f"Scanned {total} claude-response chunks from the last {window_sec / 3600:.0f}h")
    print(f"  Conversion failures: {len(failures)}")
    print(f"  Suspicious unbalanced-escape round-trips: {len(suspicious_round_trip)}")

    if verbose and failures:
        print("\n=== Conversion failures ===")
        for name, err, raw in failures[:20]:
            print(f"\n[{name}] {err}")
            print(f"  raw[:200]: {raw[:200]!r}")

    if verbose and suspicious_round_trip:
        print("\n=== Suspicious round-trips (preview) ===")
        for name, raw in suspicious_round_trip[:20]:
            print(f"\n[{name}]")
            print(f"  raw[:200]: {raw[:200]!r}")

    return len(failures)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-hours", type=float, default=24.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    failures = _scan(args.window_hours * 3600, args.verbose)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
