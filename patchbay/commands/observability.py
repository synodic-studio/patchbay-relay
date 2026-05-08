"""Observability commands: /usage, /health, /activity, /soak.

These handlers read activity logs, ccusage output, disk state, and
process bridge state for diagnostics. Helpers (`_format_tokens`,
`_bar`, `_format_uptime`, etc.) live here too — they have no callers
outside this module.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

import bridge
from patchbay import config as _config
from patchbay.config import USAGE_WEEKLY_TOKEN_CAP
from patchbay.runtime import BRIDGE_STARTED_AT


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _week_start_yyyymmdd() -> str:
    """Return the YYYYMMDD of the most recent Monday (or today if Monday)."""
    now = time.localtime()
    day_of_week = now.tm_wday  # 0 = Monday
    week_start = time.time() - day_of_week * 86400
    return time.strftime("%Y%m%d", time.localtime(week_start))


def _bar(pct: float, width: int = 12) -> str:
    """Render a percent as a unicode progress bar."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _block_time_percent(start_iso: str, end_iso: str) -> float | None:
    """Return percent of the 5h block elapsed (0..100), or None on parse error."""
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    now = datetime.now(start.tzinfo)
    span = (end - start).total_seconds()
    if span <= 0:
        return None
    return max(0.0, min(100.0, (now - start).total_seconds() / span * 100))


def _week_time_percent() -> float:
    """Return percent of the current Mon→Mon week elapsed (local time)."""
    now = datetime.now()
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_mon = monday + timedelta(days=7)
    span = (next_mon - monday).total_seconds()
    return max(0.0, min(100.0, (now - monday).total_seconds() / span * 100))


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, _ = divmod(s, 60)
    if days:
        return f"{days}d{hours}h{mins:02d}m"
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m"


def _format_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}{unit}"
        n /= 1024
    return f"{n:.1f}PiB"


def _safe_count(path: Path, pattern: str) -> int:
    """Count files matching pattern, or -1 on error (directory missing etc.)."""
    try:
        return sum(1 for _ in path.glob(pattern))
    except OSError as exc:
        bridge.logger.warning("Could not count %s/%s: %s", path, pattern, exc)
        return -1


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Claude Code quota: 5h block + week, each as a token-bar and time-bar."""
    try:
        blocks_proc, weekly_proc = await asyncio.gather(
            asyncio.to_thread(
                subprocess.run,
                ["ccusage", "blocks", "--active", "--token-limit", "max", "--json"],
                capture_output=True,
                text=True,
                timeout=15,
            ),
            asyncio.to_thread(
                subprocess.run,
                ["ccusage", "weekly", "--json", "--since", _week_start_yyyymmdd()],
                capture_output=True,
                text=True,
                timeout=15,
            ),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        await update.message.reply_text(f"usage check failed: {e}")
        return

    lines: list[str] = []

    # --- Active 5h block ---
    try:
        blocks = json.loads(blocks_proc.stdout).get("blocks", [])
        if blocks:
            b = blocks[0]
            used = b.get("totalTokens", 0)
            tls = b.get("tokenLimitStatus") or {}
            limit = tls.get("limit")
            tok_pct = tls.get("percentUsed")
            time_pct = _block_time_percent(b.get("startTime", ""), b.get("endTime", ""))
            proj = b.get("projection") or {}
            remaining = int(proj.get("remainingMinutes", 0))
            hrs, mins = divmod(remaining, 60)
            lines.append("5h block:")
            if tok_pct is not None and limit:
                lines.append(
                    f"  tokens  {_bar(tok_pct)} {tok_pct:4.1f}%  ({_format_tokens(used)}/{_format_tokens(limit)})"
                )
            else:
                lines.append(f"  tokens  {_format_tokens(used)}")
            if time_pct is not None:
                lines.append(
                    f"  time    {_bar(time_pct)} {time_pct:4.1f}%  ({hrs}h{mins:02d}m left)"
                )
        else:
            lines.append("5h block: (none)")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        lines.append(f"blocks parse error: {e}")

    lines.append("")

    # --- This week ---
    try:
        weekly = json.loads(weekly_proc.stdout).get("weekly", [])
        w = weekly[-1] if weekly else None
        used = w.get("totalTokens", 0) if w else 0
        cap = USAGE_WEEKLY_TOKEN_CAP
        wk_tok_pct = used / cap * 100 if cap else 0
        wk_time_pct = _week_time_percent()
        lines.append(f"Week (cap {_format_tokens(cap)} est):")
        lines.append(
            f"  tokens  {_bar(wk_tok_pct)} {wk_tok_pct:4.1f}%  ({_format_tokens(used)}/{_format_tokens(cap)})"
        )
        lines.append(f"  time    {_bar(wk_time_pct)} {wk_time_pct:4.1f}%")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        lines.append(f"weekly parse error: {e}")

    await update.message.reply_text("\n".join(lines))


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report bridge liveness: uptime, queues, disk free, last error."""
    from patchbay.config import BASE_DIR

    now = time.time()
    uptime = _format_uptime(now - BRIDGE_STARTED_AT)

    active = [k for k, s in bridge._sessions.items() if s.processing]

    try:
        usage = shutil.disk_usage(BASE_DIR)
        disk_free = _format_bytes(usage.free)
        disk_line = f"disk free: {disk_free} ({100 * usage.free / usage.total:.0f}%)"
    except OSError as exc:
        bridge.logger.warning("disk_usage failed for %s: %s", BASE_DIR, exc)
        disk_line = "disk free: unknown (check logs)"

    session_count = _safe_count(_config.SESSION_DIR, "*.json")
    pending_count = _safe_count(_config.PENDING_DIR, "*.json")
    failed_dir = _config.PENDING_DIR / "failed"
    failed_count = _safe_count(failed_dir, "*.json") if failed_dir.exists() else 0

    lines = [
        "bridge /health",
        f"uptime: {uptime}",
        f"active sessions: {len(active)}",
        f"session files on disk: {session_count}",
        f"pending messages: {pending_count}",
        f"failed pending (archived): {failed_count}",
        disk_line,
    ]
    if active:
        lines.append("active keys: " + ", ".join(sorted(active)))
    await update.message.reply_text("\n".join(lines))


async def cmd_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the most recent activity.jsonl entries, optionally filtered by event.

    Usage:
      /activity                  - last 8 entries, any event
      /activity <event>          - last 8 entries matching <event> (substring)
      /activity <event> <count>  - last <count> matching entries (cap 25)
    """
    parts = (update.message.text or "").split(maxsplit=2)
    event_filter = parts[1] if len(parts) > 1 else None
    try:
        max_count = max(1, min(25, int(parts[2]))) if len(parts) > 2 else 8
    except ValueError:
        max_count = 8

    if not Path(_config.ACTIVITY_LOG).exists():
        await update.message.reply_text("activity.jsonl does not exist yet.")
        return

    # Read tail of file (~last 200 lines is plenty even for max_count=25)
    try:
        with open(_config.ACTIVITY_LOG) as f:
            lines = f.readlines()[-200:]
    except OSError as exc:
        await update.message.reply_text(f"Failed to read activity.jsonl: {exc}")
        return

    matches: list[dict] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event_filter and event_filter not in entry.get("event", ""):
            continue
        matches.append(entry)
        if len(matches) >= max_count:
            break

    if not matches:
        suffix = f" matching {event_filter!r}" if event_filter else ""
        await update.message.reply_text(
            f"No activity entries{suffix} in the last 200 lines."
        )
        return

    out_lines = [
        f"activity (last {len(matches)}{', ' + event_filter if event_filter else ''}):",
    ]
    for entry in matches:
        ts = datetime.fromtimestamp(entry.get("ts", 0)).strftime("%m-%d %H:%M:%S")
        evt = entry.get("event", "?")
        # Compact one-line per entry; include up to ~3 informative fields.
        extras = []
        for k in (
            "session_key", "kind", "fixed", "error", "duration", "elapsed_ms",
            "turns_used", "exit_code", "depth", "error_type", "actions",
        ):
            if k in entry and entry[k] not in (None, "", []):
                v = entry[k]
                if isinstance(v, str) and len(v) > 80:
                    v = v[:77] + "…"
                extras.append(f"{k}={v}")
            if len(extras) >= 4:
                break
        out_lines.append(f"[{ts}] {evt}  {'  '.join(extras)}")
    await update.message.reply_text("\n".join(out_lines))


async def cmd_soak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the harness soak comparison and post the table.

    Usage:
      /soak               - all-time
      /soak 7d            - last 7 days (e.g. 24h, 30m, 3d)
      /soak 7d <session>  - filter to one session_key
    """
    parts = (update.message.text or "").split(maxsplit=2)
    since = parts[1] if len(parts) > 1 else None
    session = parts[2] if len(parts) > 2 else None

    cmd = ["uv", "run", "python", "scripts/harness_soak.py"]
    if since:
        cmd += ["--since", since]
    if session:
        cmd += ["--session", session]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(_config.BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (asyncio.TimeoutError, OSError) as exc:
        await update.message.reply_text(f"/soak failed: {exc}")
        return

    if proc.returncode != 0:
        err = (stderr or b"").decode(errors="replace")[:500]
        await update.message.reply_text(f"/soak exit {proc.returncode}: {err}")
        return

    out = (stdout or b"").decode(errors="replace").strip() or "(no output)"
    escaped = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    await update.message.reply_text(f"<pre>{escaped}</pre>", parse_mode="HTML")
