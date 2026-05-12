"""Tests for scripts/harness_soak.py — the soak comparison tool."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "harness_soak.py"


@pytest.fixture(scope="module")
def soak_module():
    spec = importlib.util.spec_from_file_location("harness_soak", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_log(tmp_path: Path, events: list[dict]) -> Path:
    log = tmp_path / "activity.jsonl"
    with log.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return log


def test_parse_since_units(soak_module):
    now = time.time()
    assert soak_module.parse_since("60s") == pytest.approx(now - 60, abs=1)
    assert soak_module.parse_since("5m") == pytest.approx(now - 300, abs=1)
    assert soak_module.parse_since("2h") == pytest.approx(now - 7200, abs=1)
    assert soak_module.parse_since("3d") == pytest.approx(now - 3 * 86400, abs=1)


def test_parse_since_rejects_garbage(soak_module):
    with pytest.raises(ValueError):
        soak_module.parse_since("forever")
    with pytest.raises(ValueError):
        soak_module.parse_since("5x")


def test_load_events_handles_missing_file(soak_module, tmp_path):
    assert soak_module.load_events(tmp_path / "nope.jsonl", None, None) == []


def test_load_events_skips_garbage_lines(soak_module, tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text(
        '{"ts": 1.0, "event": "x"}\n'
        "not json\n"
        '\n'
        '{"ts": 2.0, "event": "y"}\n'
    )
    events = soak_module.load_events(log, None, None)
    assert [e["event"] for e in events] == ["x", "y"]


def test_load_events_filters_by_since(soak_module, tmp_path):
    now = time.time()
    log = write_log(
        tmp_path,
        [
            {"ts": now - 1000, "event": "old"},
            {"ts": now - 10, "event": "new"},
        ],
    )
    events = soak_module.load_events(log, now - 100, None)
    assert [e["event"] for e in events] == ["new"]


def test_load_events_filters_by_session(soak_module, tmp_path):
    log = write_log(
        tmp_path,
        [
            {"ts": 1.0, "event": "a", "session_key": "-1000000000001_1"},
            {"ts": 2.0, "event": "b", "session_key": "-1000000000002_2"},
        ],
    )
    events = soak_module.load_events(log, None, "-1000000000002_2")
    assert [e["event"] for e in events] == ["b"]


def test_bucket_counts_invokes_per_harness(soak_module):
    events = [
        {"event": "turn_invoke", "session_key": "-1000000000001_1", "harness": "cc-cli"},
        {"event": "turn_invoke", "session_key": "-1000000000002_2", "harness": "cc-sdk"},
        {"event": "turn_invoke", "session_key": "-1000000000002_2", "harness": "cc-sdk"},
    ]
    b = soak_module.bucket_by_harness(events)
    assert b["cc-cli"]["invokes"] == 1
    assert b["cc-sdk"]["invokes"] == 2


def test_bucket_attaches_followups_to_invoke_harness(soak_module):
    """A complete event without harness inherits the invoke's harness."""
    events = [
        {"event": "turn_invoke", "session_key": "-1000000000001_1", "harness": "cc-sdk"},
        {
            "event": "turn_complete",
            "session_key": "-1000000000001_1",
            "duration": 30.0,
            "response_len": 100,
        },
    ]
    b = soak_module.bucket_by_harness(events)
    assert b["cc-sdk"]["completes"] == 1
    assert b["cc-sdk"]["durations"] == [30.0]


def test_bucket_uses_explicit_harness_when_present(soak_module):
    """If event already has harness, that wins over inheritance."""
    events = [
        {"event": "turn_invoke", "session_key": "-1000000000001_1", "harness": "cc-cli"},
        {
            "event": "turn_complete",
            "session_key": "-1000000000001_1",
            "duration": 5.0,
            "response_len": 50,
            "harness": "cc-sdk",
        },
    ]
    b = soak_module.bucket_by_harness(events)
    assert b["cc-cli"]["completes"] == 0
    assert b["cc-sdk"]["completes"] == 1


def test_bucket_classifies_outcomes(soak_module):
    events = [
        {"event": "turn_invoke", "session_key": "-1000000000000_0", "harness": "cc-sdk"},
        {"event": "turn_error", "session_key": "-1000000000000_0", "harness": "cc-sdk"},
        {"event": "turn_timeout", "session_key": "-1000000000000_0", "harness": "cc-sdk"},
        {
            "event": "process_kill",
            "session_key": "-1000000000000_0",
            "harness": "cc-sdk",
            "reason": "stalled",
        },
        {
            "event": "process_kill",
            "session_key": "-1000000000000_0",
            "harness": "cc-sdk",
            "reason": "user_kill",
        },
        {"event": "forge_handoff", "session_key": "-1000000000000_0", "harness": "cc-sdk"},
        {
            "event": "self_heal",
            "session_key": "-1000000000000_0",
            "harness": "cc-sdk",
            "kind": "claude_oom_137",
        },
        {
            "event": "self_heal",
            "session_key": "-1000000000000_0",
            "harness": "cc-sdk",
            "kind": "corrupt_session_json",
        },
    ]
    b = soak_module.bucket_by_harness(events)["cc-sdk"]
    assert b["errors"] == 1
    assert b["timeouts"] == 1
    assert b["stall_kills"] == 1
    assert b["user_kills"] == 1
    assert b["quota_hits"] == 1
    assert b["oom_self_heal"] == 1
    assert b["corrupt_session"] == 1


def test_bucket_counts_empty_responses(soak_module):
    events = [
        {"event": "turn_invoke", "session_key": "-1000000000000_0", "harness": "cc-cli"},
        {
            "event": "turn_complete",
            "session_key": "-1000000000000_0",
            "duration": 1.0,
            "response_len": 0,
        },
        {"event": "turn_invoke", "session_key": "-1000000000000_0", "harness": "cc-cli"},
        {
            "event": "turn_complete",
            "session_key": "-1000000000000_0",
            "duration": 2.0,
            "response_len": 500,
        },
    ]
    b = soak_module.bucket_by_harness(events)["cc-cli"]
    assert b["completes"] == 2
    assert b["empty_response"] == 1


def test_load_events_drops_test_fixture_session_keys(soak_module, tmp_path):
    """activity.jsonl picked up ~9000 leaked test fixture events (session_keys
    like `1_2`, `100`, `stalled`, `aaa-bbb-…`) before `_isolate_production_paths`
    landed. They distorted soak numbers (e.g. 42 of 43 `cc-cli` "manual kills"
    were from session_key=`1_2`). load_events must drop those rows."""
    log = write_log(
        tmp_path,
        [
            {"ts": 1.0, "event": "turn_invoke", "session_key": "-1003884282041_30", "harness": "cc-cli"},
            {"ts": 2.0, "event": "process_kill", "session_key": "1_2", "reason": "manual", "harness": "cc-cli"},
            {"ts": 3.0, "event": "process_kill", "session_key": "100_200", "reason": "stalled"},
            {"ts": 4.0, "event": "process_kill", "session_key": "stalled", "reason": "stalled"},
            {"ts": 5.0, "event": "turn_invoke", "session_key": "aaa-bbb-ccc", "harness": "cc-sdk"},
            {"ts": 6.0, "event": "turn_invoke", "session_key": "100", "harness": "cc-cli"},
            {"ts": 7.0, "event": "lifecycle"},  # legitimately missing session_key
        ],
    )
    events = soak_module.load_events(log, None, None)
    sks = [e.get("session_key") for e in events]
    assert sks == ["-1003884282041_30", None]


def test_is_real_session_key_classification(soak_module):
    assert soak_module.is_real_session_key("-1003884282041_30")
    assert soak_module.is_real_session_key("8289585314_0")  # DM
    assert soak_module.is_real_session_key(None)  # missing field is OK
    assert soak_module.is_real_session_key("")
    assert not soak_module.is_real_session_key("1_2")
    assert not soak_module.is_real_session_key("100")
    assert not soak_module.is_real_session_key("100_200")
    assert not soak_module.is_real_session_key("stalled")
    assert not soak_module.is_real_session_key("aaa-bbb-ccc")
    assert not soak_module.is_real_session_key(42)  # non-string


def test_bucket_does_not_flag_mop_delivered_zero_as_empty(soak_module):
    """cc-sdk-mop returns "" to the orchestrator after MOP has already pushed
    the message to Telegram. `response_len=0` paired with
    `mop_delivery_count>0` is the happy path, not a silent failure."""
    events = [
        {"event": "turn_invoke", "session_key": "-1000000000000_0", "harness": "cc-sdk-mop"},
        {
            "event": "turn_complete",
            "session_key": "-1000000000000_0",
            "harness": "cc-sdk-mop",
            "duration": 1.0,
            "response_len": 0,
            "mop_delivery_count": 1,
        },
        {"event": "turn_invoke", "session_key": "-1000000000000_0", "harness": "cc-sdk-mop"},
        {
            "event": "turn_complete",
            "session_key": "-1000000000000_0",
            "harness": "cc-sdk-mop",
            "duration": 1.0,
            "response_len": 0,
            "mop_delivery_count": 0,
        },
    ]
    b = soak_module.bucket_by_harness(events)["cc-sdk-mop"]
    assert b["completes"] == 2
    # Only the mop_delivery_count=0 complete counts as empty.
    assert b["empty_response"] == 1


def test_percentile(soak_module):
    assert soak_module.percentile([], 50) is None
    assert soak_module.percentile([1.0], 50) == 1.0
    assert soak_module.percentile([1, 2, 3, 4, 5], 50) == 3
    assert soak_module.percentile([1, 2, 3, 4, 5], 95) == 5


def test_fmt_helpers(soak_module):
    assert soak_module.fmt_duration(None) == "—"
    assert soak_module.fmt_duration(45) == "45.0s"
    assert soak_module.fmt_duration(120) == "2.0m"
    assert soak_module.fmt_pct(0, 0) == "—"
    assert soak_module.fmt_pct(1, 4) == "25.0%"


def test_render_table_handles_empty(soak_module):
    assert "no events" in soak_module.render_table({})


def test_render_table_basic(soak_module):
    events = [
        {"event": "turn_invoke", "session_key": "-1000000000000_0", "harness": "cc-sdk"},
        {
            "event": "turn_complete",
            "session_key": "-1000000000000_0",
            "duration": 10.0,
            "response_len": 100,
        },
    ]
    table = soak_module.render_table(soak_module.bucket_by_harness(events))
    assert "cc-sdk" in table
    assert "invokes" in table
    assert "completes" in table


def test_main_emits_json(soak_module, tmp_path, capsys, monkeypatch):
    log = write_log(
        tmp_path,
        [
            {
                "ts": time.time(),
                "event": "turn_invoke",
                "session_key": "-1000000000000_0",
                "harness": "cc-sdk",
            }
        ],
    )
    monkeypatch.setattr(
        "sys.argv", ["harness_soak.py", "--log", str(log), "--json"]
    )
    rc = soak_module.main()
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "cc-sdk" in parsed
    assert parsed["cc-sdk"]["invokes"] == 1


def test_main_table(soak_module, tmp_path, capsys, monkeypatch):
    log = write_log(
        tmp_path,
        [
            {
                "ts": time.time(),
                "event": "turn_invoke",
                "session_key": "-1000000000000_0",
                "harness": "cc-sdk",
            }
        ],
    )
    monkeypatch.setattr("sys.argv", ["harness_soak.py", "--log", str(log)])
    rc = soak_module.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Harness soak" in out
    assert "cc-sdk" in out


def test_bucket_reads_legacy_claude_event_names(soak_module):
    """Historical activity.jsonl uses claude_invoke/claude_complete/claude_error/
    claude_timeout. The current writer emits turn_*; soak must still parse the
    legacy names so old data isn't dropped."""
    events = [
        {"event": "claude_invoke", "session_key": "-1000000000000_0", "harness": "cc-cli"},
        {
            "event": "claude_complete",
            "session_key": "-1000000000000_0",
            "duration": 10.0,
            "response_len": 100,
            "harness": "cc-cli",
        },
        {"event": "claude_invoke", "session_key": "-1000000000002_2", "harness": "cc-sdk"},
        {"event": "claude_error", "session_key": "-1000000000002_2", "harness": "cc-sdk"},
        {"event": "claude_timeout", "session_key": "-1000000000002_2", "harness": "cc-sdk"},
    ]
    b = soak_module.bucket_by_harness(events)
    assert b["cc-cli"]["invokes"] == 1
    assert b["cc-cli"]["completes"] == 1
    assert b["cc-sdk"]["invokes"] == 1
    assert b["cc-sdk"]["errors"] == 1
    assert b["cc-sdk"]["timeouts"] == 1


def test_main_hides_noise_by_default(soak_module, tmp_path, capsys, monkeypatch):
    """legacy/unknown buckets are suppressed unless --show-noise is set."""
    log = write_log(
        tmp_path,
        [
            {
                "ts": time.time(),
                "event": "process_kill",
                "session_key": "stalled",
                "reason": "stalled",
            },
            {
                "ts": time.time(),
                "event": "turn_invoke",
                "session_key": "-1000000000000_0",
                "harness": "cc-sdk",
            },
        ],
    )
    monkeypatch.setattr("sys.argv", ["harness_soak.py", "--log", str(log)])
    soak_module.main()
    out = capsys.readouterr().out
    assert "cc-sdk" in out
    assert "legacy" not in out
    assert "unknown" not in out
