#!/usr/bin/env python3
"""Patchbay Relay: Python upstream release monitor.

Reads the currently pinned Python version from PATCHBAY_PYTHON_PIN, fetches
recent CPython release tags from GitHub, and either recommends an upgrade
with rationale or marks the release as safely ignorable.

The point is to flip the model from 'detect after Homebrew silently broke
TCC permissions' to 'never let an unplanned upgrade happen.' Pin Python.
Watch upstream releases. Upgrade only when a changelog actually warrants
the cost, and do it deliberately.

Usage:
    PATCHBAY_PYTHON_PIN=3.13.0 python3 scripts/python_check.py
    PATCHBAY_PYTHON_PIN=3.13.0 python3 scripts/python_check.py --auto-ignore

Optional environment:
    PATCHBAY_PYTHON_KEYWORDS  comma-separated keywords that warrant upgrade
                              (default: security, CVE, vulnerability, RCE, etc.)
    PATCHBAY_PYTHON_IGNORE    path to ignore-list JSON
                              (default: ~/.config/patchbay/python-ignore.json)
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

DEFAULT_KEYWORDS = [
    "security",
    "CVE",
    "vulnerability",
    "buffer overflow",
    "remote code execution",
    "RCE",
    "denial of service",
    "DoS",
]
DEFAULT_IGNORE_PATH = Path.home() / ".config" / "patchbay" / "python-ignore.json"
GITHUB_TAGS_URL = "https://api.github.com/repos/python/cpython/tags?per_page=50"
NEWS_URL_TEMPLATE = "https://raw.githubusercontent.com/python/cpython/v{version}/Misc/NEWS.d/{version}.rst"

# Tags like v3.13.0a1, v3.13.0b2, v3.13.0rc1 are pre-releases we skip.
PRERELEASE_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+(a|b|rc|c)\d+$", re.IGNORECASE)
STABLE_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(tag: str) -> tuple[int, int, int]:
    """Parse a stable tag like 'v3.13.2' or '3.13.2' into (major, minor, patch).

    Returns (0, 0, 0) for pre-release or malformed tags.
    """
    match = STABLE_VERSION_PATTERN.match(tag.strip())
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def version_string(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def load_ignore_list(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def save_ignore_list(path: Path, versions: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(versions), indent=2) + "\n")


def http_get(url: str, timeout: int = 15) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "patchbay-python-check"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_tags() -> list[str]:
    raw = http_get(GITHUB_TAGS_URL)
    return [entry["name"] for entry in json.loads(raw)]


def fetch_changelog(version: tuple[int, int, int]) -> str:
    """Fetch the upstream NEWS.d/X.Y.Z.rst entry. Returns '' on miss."""
    url = NEWS_URL_TEMPLATE.format(version=version_string(version))
    try:
        return http_get(url)
    except Exception:
        return ""


def scan_changelog(body: str, keywords: list[str]) -> list[str]:
    """Return one matching line per keyword that appears in body.

    Matching is word-boundary aware and case-insensitive so 'RCE' does not
    falsely match 'source' and 'CVE' does not falsely match 'concrete'.
    Useful as a rationale string in the upgrade recommendation.
    """
    patterns = [
        (keyword, re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE))
        for keyword in keywords
    ]
    hits: list[str] = []
    seen_keywords: set[str] = set()
    for line in body.splitlines():
        for keyword, pattern in patterns:
            if keyword in seen_keywords:
                continue
            if pattern.search(line):
                trimmed = line.strip()
                if trimmed.startswith(".. section:"):
                    continue  # RST section markers are noise
                if len(trimmed) > 140:
                    trimmed = trimmed[:137] + "..."
                hits.append(f"  matches '{keyword}': {trimmed}")
                seen_keywords.add(keyword)
    return hits


def filter_candidate_versions(
    tags: list[str],
    pinned: tuple[int, int, int],
    ignore: set[str],
) -> list[tuple[int, int, int]]:
    """Return stable versions strictly newer than `pinned` and not in `ignore`."""
    candidates: set[tuple[int, int, int]] = set()
    for tag in tags:
        if PRERELEASE_PATTERN.match(tag):
            continue
        version = parse_version(tag)
        if version == (0, 0, 0):
            continue
        if version <= pinned:
            continue
        if version_string(version) in ignore:
            continue
        candidates.add(version)
    return sorted(candidates)


def classify_candidates(
    candidates: list[tuple[int, int, int]],
    keywords: list[str],
) -> tuple[list[tuple[tuple[int, int, int], list[str]]], list[tuple[int, int, int]]]:
    """Split candidates into (recommended, safe_to_ignore) by changelog scan."""
    recommended: list[tuple[tuple[int, int, int], list[str]]] = []
    safe_to_ignore: list[tuple[int, int, int]] = []
    for version in candidates:
        body = fetch_changelog(version)
        hits = scan_changelog(body, keywords) if body else []
        if hits:
            recommended.append((version, hits))
        else:
            safe_to_ignore.append(version)
    return recommended, safe_to_ignore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--auto-ignore",
        action="store_true",
        help="add safe-to-ignore versions to the ignore list automatically",
    )
    args = parser.parse_args(argv)

    pin = os.environ.get("PATCHBAY_PYTHON_PIN", "").strip()
    if not pin:
        print(
            "error: set PATCHBAY_PYTHON_PIN to your pinned Python version, e.g. '3.13.0'",
            file=sys.stderr,
        )
        return 2

    pinned = parse_version(pin)
    if pinned == (0, 0, 0):
        print(f"error: could not parse PATCHBAY_PYTHON_PIN={pin!r}", file=sys.stderr)
        return 2

    keywords_env = os.environ.get("PATCHBAY_PYTHON_KEYWORDS", "")
    if keywords_env:
        keywords = [k.strip() for k in keywords_env.split(",") if k.strip()]
    else:
        keywords = DEFAULT_KEYWORDS

    ignore_path = Path(os.environ.get("PATCHBAY_PYTHON_IGNORE", str(DEFAULT_IGNORE_PATH)))
    ignore = load_ignore_list(ignore_path)

    try:
        tags = fetch_tags()
    except Exception as exc:
        print(f"error: could not fetch CPython tags: {exc}", file=sys.stderr)
        return 1

    candidates = filter_candidate_versions(tags, pinned, ignore)

    if not candidates:
        print(f"Pinned at {pin}. No newer stable releases.")
        return 0

    recommended, safe_to_ignore = classify_candidates(candidates, keywords)

    if not recommended:
        print(
            f"Pinned at {pin}. {len(safe_to_ignore)} newer release(s) found, "
            f"none match the upgrade keywords."
        )
        if args.auto_ignore:
            for version in safe_to_ignore:
                ignore.add(version_string(version))
            save_ignore_list(ignore_path, ignore)
            print(f"Added {len(safe_to_ignore)} release(s) to ignore list at {ignore_path}.")
        return 0

    print(f"Pinned at {pin}. Upgrade recommended.\n")
    for version, hits in recommended:
        version_str = version_string(version)
        print(f"  Python {version_str}")
        for hit in hits:
            print(hit)
        print(f"  Release notes: https://docs.python.org/release/{version_str}/")
        print()

    if safe_to_ignore:
        print(f"({len(safe_to_ignore)} other newer release(s) had no matching keywords.)")
        if args.auto_ignore:
            for version in safe_to_ignore:
                ignore.add(version_string(version))
            save_ignore_list(ignore_path, ignore)
            print(f"Added {len(safe_to_ignore)} release(s) to ignore list at {ignore_path}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
