#!/usr/bin/env python3
"""Compare harness backends from activity.jsonl.

Buckets per-turn events by `harness` field and prints a side-by-side
table of turn counts, outcomes, durations, and self-heal incidents.

Usage:
    uv run scripts/harness_soak.py                       # all-time, all harnesses
    uv run scripts/harness_soak.py --since 7d            # last 7 days
    uv run scripts/harness_soak.py --session <key>       # one chat only
    uv run scripts/harness_soak.py --json                # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO_ROOT / "activity.jsonl"


def parse_since(spec: str) -> float:
    """'7d', '24h', '30m' → cutoff timestamp (seconds since epoch)."""
    m = re.fullmatch(r"(\d+)([smhd])", spec.strip())
    if not m:
        raise ValueError(f"bad --since: {spec!r} (use 30m, 24h, 7d, ...)")
    n, unit = int(m.group(1)), m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return time.time() - n * seconds


def load_events(
    log_path: Path,
    since_ts: float | None,
    session_filter: str | None,
) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in log_path.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts = ev.get("ts")
        if since_ts is not None and (not isinstance(ts, (int, float)) or ts < since_ts):
            continue
        if session_filter and ev.get("session_key") != session_filter:
            continue
        out.append(ev)
    return out


def bucket_by_harness(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group events by harness, attaching invoke harness to follow-up events.

    Per-turn events emitted before phase 1c lack a harness field. We
    propagate the most recent invoke's harness within the same session to
    cover those rows so duration/error attribution stays correct.
    """
    last_invoke_harness: dict[str, str] = {}
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "invokes": 0,
            "completes": 0,
            "errors": 0,
            "timeouts": 0,
            "stall_kills": 0,
            "user_kills": 0,
            "quota_hits": 0,
            "oom_self_heal": 0,
            "corrupt_session": 0,
            "empty_response": 0,
            "durations": [],
            "response_lens": [],
        }
    )

    for ev in events:
        event = ev.get("event")
        sk = ev.get("session_key", "")
        harness = ev.get("harness")
        if event in ("turn_invoke", "claude_invoke"):
            harness = ev.get("harness") or "unknown"
            last_invoke_harness[sk] = harness
            buckets[harness]["invokes"] += 1
            continue

        if not harness:
            harness = last_invoke_harness.get(sk, "legacy")

        b = buckets[harness]
        if event in ("turn_complete", "claude_complete"):
            b["completes"] += 1
            d = ev.get("duration")
            if isinstance(d, (int, float)):
                b["durations"].append(d)
            rl = ev.get("response_len")
            if isinstance(rl, int):
                b["response_lens"].append(rl)
                if rl == 0:
                    b["empty_response"] += 1
        elif event in ("turn_error", "claude_error"):
            b["errors"] += 1
        elif event in ("turn_timeout", "claude_timeout"):
            b["timeouts"] += 1
        elif event == "process_kill":
            reason = ev.get("reason", "")
            if reason == "stalled":
                b["stall_kills"] += 1
            else:
                b["user_kills"] += 1
        elif event == "forge_handoff":
            b["quota_hits"] += 1
        elif event == "self_heal":
            kind = ev.get("kind", "")
            if kind == "claude_oom_137":
                b["oom_self_heal"] += 1
            elif kind == "corrupt_session_json":
                b["corrupt_session"] += 1
    return buckets


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100) * (len(s) - 1)))))
    return s[k]


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}m"


def fmt_pct(num: int, denom: int) -> str:
    if denom == 0:
        return "—"
    return f"{100 * num / denom:.1f}%"


def render_table(buckets: dict[str, dict[str, Any]]) -> str:
    if not buckets:
        return "(no events in window)"

    harnesses = sorted(buckets.keys())
    rows: list[tuple[str, list[str]]] = []

    def add(label: str, values: list[str]) -> None:
        rows.append((label, values))

    add("invokes", [str(buckets[h]["invokes"]) for h in harnesses])
    add("completes", [str(buckets[h]["completes"]) for h in harnesses])
    add("  empty (0 bytes)", [str(buckets[h]["empty_response"]) for h in harnesses])
    add("errors", [str(buckets[h]["errors"]) for h in harnesses])
    add("timeouts", [str(buckets[h]["timeouts"]) for h in harnesses])
    add("stall kills", [str(buckets[h]["stall_kills"]) for h in harnesses])
    add("user kills (/kill)", [str(buckets[h]["user_kills"]) for h in harnesses])
    add("quota handoffs", [str(buckets[h]["quota_hits"]) for h in harnesses])
    add("OOM self-heal", [str(buckets[h]["oom_self_heal"]) for h in harnesses])
    add("corrupt-session heal", [str(buckets[h]["corrupt_session"]) for h in harnesses])

    add("", ["" for _ in harnesses])
    add(
        "complete-rate",
        [fmt_pct(buckets[h]["completes"], buckets[h]["invokes"]) for h in harnesses],
    )
    add(
        "error-rate",
        [fmt_pct(buckets[h]["errors"], buckets[h]["invokes"]) for h in harnesses],
    )
    add(
        "empty-on-complete",
        [
            fmt_pct(buckets[h]["empty_response"], buckets[h]["completes"])
            for h in harnesses
        ],
    )

    add("", ["" for _ in harnesses])
    add(
        "duration p50",
        [fmt_duration(percentile(buckets[h]["durations"], 50)) for h in harnesses],
    )
    add(
        "duration p95",
        [fmt_duration(percentile(buckets[h]["durations"], 95)) for h in harnesses],
    )
    add(
        "duration mean",
        [
            fmt_duration(
                statistics.fmean(buckets[h]["durations"])
                if buckets[h]["durations"]
                else None
            )
            for h in harnesses
        ],
    )

    label_w = max(len(r[0]) for r in rows)
    col_w = max(
        max((len(v) for v in row[1]), default=0) for row in rows
    )
    col_w = max(col_w, max(len(h) for h in harnesses))

    def line(label: str, cells: list[str]) -> str:
        return f"  {label.ljust(label_w)}  " + "  ".join(
            c.rjust(col_w) for c in cells
        )

    header = line("metric", harnesses)
    sep = "  " + "-" * (label_w + 2 + len(harnesses) * (col_w + 2))
    body = "\n".join(line(label, cells) for label, cells in rows)
    return f"{header}\n{sep}\n{body}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"path to activity.jsonl (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="only events newer than e.g. 7d, 24h, 30m",
    )
    parser.add_argument(
        "--session",
        type=str,
        default=None,
        help="filter to one session_key (e.g. -100123456789_42)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of table",
    )
    parser.add_argument(
        "--show-noise",
        action="store_true",
        help="include 'legacy' and 'unknown' buckets (default: hide test noise)",
    )
    args = parser.parse_args()

    since_ts = parse_since(args.since) if args.since else None
    events = load_events(args.log, since_ts, args.session)
    buckets = bucket_by_harness(events)
    if not args.show_noise:
        buckets = {h: b for h, b in buckets.items() if h not in ("legacy", "unknown")}

    if args.json:
        out: dict[str, Any] = {}
        for h, b in buckets.items():
            out[h] = {
                "invokes": b["invokes"],
                "completes": b["completes"],
                "errors": b["errors"],
                "timeouts": b["timeouts"],
                "stall_kills": b["stall_kills"],
                "user_kills": b["user_kills"],
                "quota_hits": b["quota_hits"],
                "oom_self_heal": b["oom_self_heal"],
                "corrupt_session": b["corrupt_session"],
                "empty_response": b["empty_response"],
                "duration_p50": percentile(b["durations"], 50),
                "duration_p95": percentile(b["durations"], 95),
                "duration_mean": (
                    statistics.fmean(b["durations"]) if b["durations"] else None
                ),
            }
        json.dump(out, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    window = "all-time"
    if args.since:
        window = f"last {args.since}"
    scope = f", session={args.session}" if args.session else ""
    print(f"Harness soak — {window}{scope} ({len(events)} events)")
    print()
    print(render_table(buckets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
