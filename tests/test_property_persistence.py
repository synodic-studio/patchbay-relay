"""Property tests for the file-backed persistence layer.

These tests use Hypothesis to drive randomized writes against the four
JSON-backed dicts (`efforts`, `projects`, `sessions`, `outbound`) and
assert no corruption — every successful write is readable, every cap is
honored, every roundtrip preserves data.

See `docs/STARGATE-IMPROVEMENT-PLAN.md` §3b (CTB-dnc).
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import stargate.efforts
import stargate.outbound
import stargate.projects
import stargate.sessions

# A "session key" matches stargate.config.SESSION_KEY_RE: alphanumeric,
# underscore, hyphen. Use a narrow alphabet so generated keys are valid
# both as map keys and (for sessions.py) as filename components.
# Lowercase-ASCII only. macOS HFS+/APFS filesystems are case-insensitive
# *and* normalize unicode, so "B" and "b" or "Ŏ" and "ŏ" can collapse onto
# the same filename and break the roundtrip when two keys are distinct in
# Python but identical on disk. Constraining the alphabet here keeps the
# property test focused on the persistence layer's invariants, not on
# filesystem quirks.
session_keys = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789_-",
    ),
    min_size=1,
    max_size=24,
).filter(lambda s: ".." not in s)

efforts_values = st.sampled_from(stargate.efforts.VALID_EFFORTS)

# Project paths are arbitrary strings; the layer doesn't validate them.
project_paths = st.text(min_size=1, max_size=40).filter(
    lambda s: "\x00" not in s and not s.startswith("/")
)

# Session IDs are uuid-like strings in practice, but the layer treats
# them as opaque text — exercise the full string range.
session_ids = st.text(min_size=1, max_size=64).filter(lambda s: "\x00" not in s)


# Hypothesis settings: keep deterministic and reasonably fast.
_FAST = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ---------------------------------------------------------------------------
# Fixtures: redirect each module's storage paths to tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_efforts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        stargate.efforts, "CHAT_EFFORTS_FILE", tmp_path / "chat_efforts.json"
    )
    monkeypatch.setattr(
        stargate.efforts, "_EFFORTS_LOCK_FILE", tmp_path / ".chat_efforts.lock"
    )
    return tmp_path


@pytest.fixture
def isolated_projects(tmp_path, monkeypatch):
    projects_file = tmp_path / "chat_projects.json"
    monkeypatch.setattr(stargate.projects, "CHAT_PROJECTS_FILE", projects_file)
    monkeypatch.setattr(
        stargate.projects, "_PROJECTS_LOCK_FILE", tmp_path / ".chat_projects.lock"
    )
    monkeypatch.setattr(stargate.projects, "WORKING_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(stargate.sessions, "PENDING_DIR", tmp_path / "pending")
    (tmp_path / "pending").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def isolated_outbound(tmp_path, monkeypatch):
    monkeypatch.setattr(stargate.outbound, "OUTBOUND_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# efforts.py — randomized roundtrip
# ---------------------------------------------------------------------------


class TestEffortsProperty:
    @_FAST
    @given(
        ops=st.lists(
            st.tuples(session_keys, st.one_of(efforts_values, st.none())),
            min_size=1,
            max_size=30,
        )
    )
    def test_set_get_roundtrip(self, isolated_efforts, ops):
        """For any sequence of set ops, get returns the last value set per key."""
        expected: dict[str, str | None] = {}
        for key, val in ops:
            stargate.efforts.set_chat_effort(key, val)
            expected[key] = val

        for key, val in expected.items():
            assert stargate.efforts.get_chat_effort(key) == val

    @_FAST
    @given(keys=st.lists(session_keys, min_size=2, max_size=10, unique=True))
    def test_setting_one_does_not_disturb_others(self, isolated_efforts, keys):
        for key in keys:
            stargate.efforts.set_chat_effort(key, "high")
        # Now flip one
        target = keys[0]
        stargate.efforts.set_chat_effort(target, "low")
        assert stargate.efforts.get_chat_effort(target) == "low"
        for k in keys[1:]:
            assert stargate.efforts.get_chat_effort(k) == "high"


# ---------------------------------------------------------------------------
# projects.py — randomized roundtrip
# ---------------------------------------------------------------------------


class TestProjectsProperty:
    @_FAST
    @given(
        ops=st.lists(
            st.tuples(session_keys, st.one_of(project_paths, st.none())),
            min_size=1,
            max_size=30,
        )
    )
    def test_set_get_roundtrip(self, isolated_projects, ops):
        expected: dict[str, str | None] = {}
        for key, path in ops:
            stargate.projects.set_chat_project(key, path)
            expected[key] = path

        for key, path in expected.items():
            if path is None:
                # set_chat_project(None) clears the key
                cwd = stargate.projects.get_chat_working_dir(key)
                # Cleared keys fall back to WORKING_DIR
                assert cwd == str(isolated_projects)
            else:
                cwd = stargate.projects.get_chat_working_dir(key)
                assert cwd == f"{isolated_projects}/{path}"


# ---------------------------------------------------------------------------
# sessions.py — randomized save/load roundtrip
# ---------------------------------------------------------------------------


class TestSessionsProperty:
    @_FAST
    @given(
        ops=st.lists(
            st.tuples(session_keys, session_ids),
            min_size=1,
            max_size=20,
        )
    )
    def test_save_load_roundtrip(self, isolated_sessions, ops):
        expected: dict[str, str] = {}
        for key, sid in ops:
            stargate.sessions.save_session_id(key, sid)
            expected[key] = sid

        for key, sid in expected.items():
            assert stargate.sessions.get_session_id(key) == sid

    @_FAST
    @given(
        key=session_keys,
        sid=session_ids,
        garbage=st.text(min_size=0, max_size=200),
    )
    def test_garbage_in_session_file_is_quarantined(
        self, isolated_sessions, key, sid, garbage
    ):
        """A corrupt file should NOT poison the chat: get returns None, file is moved aside."""
        # Plant a session, then overwrite with garbage.
        stargate.sessions.save_session_id(key, sid)
        session_file = isolated_sessions / f"{key}.json"
        session_file.write_text(garbage)
        try:
            parsed = json.loads(garbage)
            valid = isinstance(parsed, dict) and "session_id" in parsed and "last_active" in parsed
        except (json.JSONDecodeError, ValueError):
            valid = False

        result = stargate.sessions.get_session_id(key)
        if not valid:
            # Corrupt → quarantined → returns None
            assert result is None
            assert not session_file.exists()
            assert (isolated_sessions / ".quarantine").is_dir()


# ---------------------------------------------------------------------------
# outbound.py — randomized prune invariants
# ---------------------------------------------------------------------------


class TestOutboundPruneProperty:
    @_FAST
    @given(
        notifications=st.lists(st.text(min_size=1, max_size=40), min_size=0, max_size=30),
        responses=st.lists(st.text(min_size=1, max_size=40), min_size=0, max_size=80),
    )
    def test_caps_independent(self, isolated_outbound, notifications, responses):
        """Notifications and responses are pruned independently with their own caps."""
        key = "session_test"
        for text in notifications:
            stargate.outbound.log_outbound(key, text, "buddy")
        for i, text in enumerate(responses):
            stargate.outbound.log_outbound_response(
                session_key=key,
                chunk_index=i,
                chunk_total=len(responses),
                raw=text,
                md=None,
                parse_mode="plain",
                status="ok",
            )

        path = isolated_outbound / f"{key}.jsonl"
        if not path.exists():
            assert not notifications and not responses
            return

        lines = [line for line in path.read_text().splitlines() if line]
        n_count = sum(
            1 for line in lines if json.loads(line).get("source") != "claude-response"
        )
        r_count = sum(
            1 for line in lines if json.loads(line).get("source") == "claude-response"
        )
        assert n_count <= stargate.outbound.MAX_ENTRIES
        assert r_count <= stargate.outbound.MAX_RESPONSE_ENTRIES
        # And every line round-trips through json.loads
        for line in lines:
            json.loads(line)  # raises if corrupt

    @_FAST
    @given(texts=st.lists(st.text(min_size=1, max_size=40), min_size=1, max_size=15))
    def test_get_recent_returns_in_chronological_order(self, isolated_outbound, texts):
        """The list returned by get_recent_outbound is chronological (newest last)."""
        key = "chrono_test"
        for text in texts:
            stargate.outbound.log_outbound(key, text, "feathers")
            time.sleep(0.0005)  # ensure ts ordering

        recent = stargate.outbound.get_recent_outbound(key)
        timestamps = [entry["ts"] for entry in recent]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# Concurrent writers — no corruption under thread-stress
# ---------------------------------------------------------------------------


class TestConcurrentWriters:
    """Threads can't fully exercise fcntl (per-process), but they catch
    bugs in the read-modify-write pattern under interleaved scheduling."""

    def test_efforts_concurrent_writes_no_corruption(self, isolated_efforts):
        keys = [f"key_{i}" for i in range(20)]

        def writer(my_keys):
            for k in my_keys:
                stargate.efforts.set_chat_effort(k, "high")

        # Split keys across threads
        threads = [
            threading.Thread(target=writer, args=(keys[i::4],)) for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # File parses, every key landed
        for k in keys:
            assert stargate.efforts.get_chat_effort(k) == "high"

    def test_projects_concurrent_writes_no_corruption(self, isolated_projects):
        keys = [f"key_{i}" for i in range(20)]

        def writer(my_keys):
            for k in my_keys:
                stargate.projects.set_chat_project(k, f"path_{k}")

        threads = [
            threading.Thread(target=writer, args=(keys[i::4],)) for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The file should parse cleanly; no half-writes
        data = json.loads((isolated_projects / "chat_projects.json").read_text())
        for k in keys:
            assert data.get(k) == f"path_{k}"

    def test_outbound_concurrent_appends_no_loss_under_cap(self, isolated_outbound):
        """When total writes < cap, none are lost across threads."""
        key = "concurrent_outbound"
        per_thread = 2  # 4 threads × 2 = 8, well under MAX_ENTRIES=10

        def writer(thread_id):
            for i in range(per_thread):
                stargate.outbound.log_outbound(key, f"t{thread_id}_msg{i}", "buddy")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        recent = stargate.outbound.get_recent_outbound(key)
        # All 8 messages survived
        assert len(recent) == 4 * per_thread
        texts = {e["text"] for e in recent}
        expected = {f"t{i}_msg{j}" for i in range(4) for j in range(per_thread)}
        assert texts == expected
