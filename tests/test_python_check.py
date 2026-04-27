"""Unit tests for scripts/python_check.py.

Covers the pure helper functions: version parsing, candidate filtering,
keyword scanning, and ignore-list round-tripping. The HTTP-fetching paths
are not covered here; they are exercised manually against live upstream.
"""

import importlib.util
import json
from pathlib import Path

import pytest

# Load the script as a module since scripts/ isn't on sys.path by default.
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "python_check.py"
SPEC = importlib.util.spec_from_file_location("python_check", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
python_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(python_check)


class TestParseVersion:
    @pytest.mark.parametrize(
        "tag,expected",
        [
            ("3.13.2", (3, 13, 2)),
            ("v3.13.2", (3, 13, 2)),
            ("v3.14.0", (3, 14, 0)),
            ("3.0.0", (3, 0, 0)),
        ],
    )
    def test_parses_stable_versions(self, tag: str, expected: tuple[int, int, int]) -> None:
        assert python_check.parse_version(tag) == expected

    @pytest.mark.parametrize(
        "tag",
        [
            "v3.13.0a1",
            "v3.13.0b2",
            "v3.13.0rc1",
            "v3.15.0a8",
            "garbage",
            "",
            "3.13",
            "v3",
        ],
    )
    def test_rejects_pre_releases_and_malformed(self, tag: str) -> None:
        assert python_check.parse_version(tag) == (0, 0, 0)


class TestVersionString:
    def test_round_trip(self) -> None:
        for tag in ["3.13.2", "v3.14.0"]:
            parsed = python_check.parse_version(tag)
            assert python_check.version_string(parsed) == tag.lstrip("v")


class TestFilterCandidateVersions:
    def test_filters_older_pre_release_and_ignored(self) -> None:
        tags = [
            "v3.15.0a8",  # pre-release, skip
            "v3.14.4",  # newer stable, keep
            "v3.14.3",  # newer stable, keep
            "v3.13.2",  # newer stable, keep
            "v3.13.1",  # newer stable, keep
            "v3.13.0",  # equal to pin, skip
            "v3.12.0",  # older, skip
            "garbage",  # malformed, skip
        ]
        pinned = (3, 13, 0)
        ignore = {"3.14.3"}  # explicitly ignored
        candidates = python_check.filter_candidate_versions(tags, pinned, ignore)
        assert candidates == [(3, 13, 1), (3, 13, 2), (3, 14, 4)]

    def test_dedupes_repeated_tags(self) -> None:
        tags = ["v3.13.1", "3.13.1", "v3.13.1"]
        candidates = python_check.filter_candidate_versions(tags, (3, 13, 0), set())
        assert candidates == [(3, 13, 1)]


class TestScanChangelog:
    def test_word_boundary_avoids_false_positives(self) -> None:
        # 'RCE' substring appears inside 'source' but should not match.
        body = "Fix an issue in :meth:`email.policy.EmailPolicy.header_source_parse`"
        hits = python_check.scan_changelog(body, ["RCE"])
        assert hits == []

    def test_matches_real_security_keywords(self) -> None:
        body = (
            ".. section: Security\n"
            "Addresses :cve:`2024-12718` and a buffer overflow.\n"
            "Fix a potential denial of service in imaplib."
        )
        hits = python_check.scan_changelog(body, ["CVE", "buffer overflow", "denial of service"])
        assert len(hits) == 3
        joined = "\n".join(hits)
        assert "CVE" in joined
        assert "buffer overflow" in joined
        assert "denial of service" in joined

    def test_skips_rst_section_markers(self) -> None:
        # The keyword 'security' appears in the section header line; we should
        # skip those because every release has them and they carry no signal.
        body = ".. section: Security\nReal security fix here for the foo() bug."
        hits = python_check.scan_changelog(body, ["security"])
        assert len(hits) == 1
        assert "Real security fix" in hits[0]

    def test_one_hit_per_keyword(self) -> None:
        body = "security one\nsecurity two\nsecurity three\n"
        hits = python_check.scan_changelog(body, ["security"])
        assert len(hits) == 1


class TestIgnoreList:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "ignore.json"
        original = {"3.13.5", "3.13.7", "3.14.1"}
        python_check.save_ignore_list(path, original)
        loaded = python_check.load_ignore_list(path)
        assert loaded == original

    def test_load_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        assert python_check.load_ignore_list(tmp_path / "missing.json") == set()

    def test_load_returns_empty_for_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("not valid json {{{")
        assert python_check.load_ignore_list(path) == set()


class TestClassifyCandidates:
    def test_recommended_vs_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bodies = {
            (3, 13, 1): "Just a typo fix in the docs.",
            (3, 13, 2): "Addresses :cve:`2024-1234` in urllib.",
            (3, 13, 3): "Performance improvement in dict.",
        }

        def fake_fetch(version: tuple[int, int, int]) -> str:
            return bodies.get(version, "")

        monkeypatch.setattr(python_check, "fetch_changelog", fake_fetch)
        recommended, safe = python_check.classify_candidates(
            list(bodies.keys()),
            ["CVE"],
        )
        assert [version for version, _ in recommended] == [(3, 13, 2)]
        assert safe == [(3, 13, 1), (3, 13, 3)]
