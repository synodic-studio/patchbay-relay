"""Configuration: environment variables, constants, and paths.

All configuration is centralized here. Other modules import from this module
rather than reading os.environ directly.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")


# ---------------------------------------------------------------------------
# Env-var validation helpers
#
# Misconfigured .env files used to surface as unhelpful ValueError tracebacks
# at import time. Now each env read has its own guard that either returns a
# sane value or exits with a clear human-readable error on stderr — the
# bridge refuses to start rather than running half-broken.
# ---------------------------------------------------------------------------


def _fatal_config(msg: str) -> None:
    """Print a clear error to stderr and exit. No self-heal when we can't
    guess what the user meant."""
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _env_int(name: str, default: str, min_value: int | None = None) -> int:
    """Parse an int env var with validation. Fatal if unparseable or below min."""
    raw = os.environ.get(name, default)
    try:
        value = int(raw)
    except ValueError:
        _fatal_config(f"{name}={raw!r} is not an integer. Set {name} to a valid integer in .env (default: {default}).")
    if min_value is not None and value < min_value:
        _fatal_config(f"{name}={value} is below the minimum ({min_value}). Set a larger value in .env.")
    return value


def _env_existing_path(name: str, default: str, description: str) -> str:
    """Read an env var holding a directory path and fatal-exit if missing.
    User-expansion ('~') is applied."""
    raw = os.environ.get(name, default)
    expanded = os.path.expanduser(raw)
    if not os.path.isdir(expanded):
        _fatal_config(
            f"{name}={raw!r} — {description} does not exist at {expanded}. "
            f"Create the directory or point {name} at an existing one."
        )
    return expanded


def _resolve_claude_binary(configured: str) -> str:
    """Find the claude CLI. Try the configured path first; if missing, walk
    a short list of known fallbacks. Fatal-exit if none work — the bridge
    is useless without a working Claude CLI."""
    expanded = os.path.expanduser(configured)
    if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
        return expanded

    # Self-heal: try the two canonical install locations plus $PATH lookup.
    fallbacks = [
        os.path.expanduser("~/.local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    path_lookup = shutil.which("claude")
    if path_lookup:
        fallbacks.append(path_lookup)

    for candidate in fallbacks:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            if candidate != expanded:
                logger_bootstrap = logging.getLogger("bridge")
                logger_bootstrap.warning(
                    "CLAUDE_PATH=%s not executable; falling back to %s",
                    configured,
                    candidate,
                )
            return candidate

    _fatal_config(
        f"claude CLI not found. Tried CLAUDE_PATH={configured!r} and common "
        f"locations ({', '.join(fallbacks)}). Install Claude Code or set "
        f"CLAUDE_PATH to the correct binary."
    )


class _SecretStr:
    """Wraps a secret string so it never appears in tracebacks or repr output."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "***REDACTED***"

    def __str__(self) -> str:
        return "***REDACTED***"

    def __bool__(self) -> bool:
        return bool(self._value)

    def reveal(self) -> str:
        """Return the raw secret value. Call only where the plaintext is required."""
        return self._value


def _load_bot_token() -> _SecretStr:
    """Load the Telegram bot token from pass (password-store) or env var."""
    try:
        result = subprocess.run(
            ["pass", "show", "telegram-bot-token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return _SecretStr(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return _SecretStr(os.environ.get("TELEGRAM_BOT_TOKEN", ""))


# --- Bot token ---
BOT_TOKEN = _load_bot_token()
if not BOT_TOKEN:
    print(
        "ERROR: BOT_TOKEN is empty. Set TELEGRAM_BOT_TOKEN in the environment "
        "or ensure 'telegram-bot-token' is accessible via `pass show telegram-bot-token`.",
        file=sys.stderr,
    )
    sys.exit(1)

# --- Paths ---
# CLAUDE_PATH self-heals: we try the configured value first, then a short
# list of canonical install locations, then $PATH. Hard fail if none work.
CLAUDE_PATH = _resolve_claude_binary(os.environ.get("CLAUDE_PATH", "~/.local/bin/claude"))
WORKING_DIR = _env_existing_path("CLAUDE_WORKING_DIR", "~/Developer", "Claude working directory")
PA_PLUGIN_DIR = _env_existing_path("PA_PLUGIN_DIR", "~/Developer/Fanta", "Fanta plugin directory")

BASE_DIR = Path(__file__).parent.parent
SESSION_DIR = BASE_DIR / "sessions"
SESSION_DIR.mkdir(exist_ok=True)
PENDING_DIR = BASE_DIR / "pending"
PENDING_DIR.mkdir(exist_ok=True)
AIDER_HISTORY_DIR = BASE_DIR / "aider-history"
AIDER_HISTORY_DIR.mkdir(exist_ok=True)
RESTART_NOTIFY_FILE = BASE_DIR / "restart_notify.json"
CHAT_PROJECTS_FILE = BASE_DIR / "chat_projects.json"
FORGE_QUEUE_DIR = Path(PA_PLUGIN_DIR) / "agents" / "dev" / "forge" / "queue"
ACTIVITY_LOG = BASE_DIR / "activity.jsonl"
PHOTO_DIR = Path(tempfile.gettempdir()) / "claude-telegram-photos"
PHOTO_DIR.mkdir(exist_ok=True)
DOC_DIR = Path(tempfile.gettempdir()) / "claude-telegram-docs"
DOC_DIR.mkdir(exist_ok=True)

# --- Tunables ---
SESSION_EXPIRY = _env_int("SESSION_EXPIRY", "259200", min_value=1)  # 3 days
MAX_TIMEOUT = _env_int("MAX_TIMEOUT", "2700", min_value=1)  # 45 min safety valve
MAX_TURNS = _env_int("MAX_TURNS", "500", min_value=1)
MAX_WORKERS = _env_int("MAX_WORKERS", "4", min_value=1)

# --- Harness ---
# Pluggable agent backend. cc-cli wraps `claude -p`, cc-sdk uses the Claude
# Agent SDK, pi wraps badlogicgames/pi (multi-model). Per-chat override lives
# in chat_projects.json under the "harness" key; this value is the fallback
# when a topic has no override.
VALID_HARNESSES = ("cc-cli", "cc-sdk", "pi", "aider", "opencode")
DEFAULT_HARNESS = os.environ.get("STARGATE_DEFAULT_HARNESS", "cc-cli")
if DEFAULT_HARNESS not in VALID_HARNESSES:
    raise SystemExit(
        f"STARGATE_DEFAULT_HARNESS={DEFAULT_HARNESS!r} is not one of {VALID_HARNESSES}"
    )

# /usage weekly-cap estimate. Anthropic does not publish a weekly token cap
# for Max plans — the closest public data (Portkey's community-measured
# numbers) quotes hours/week, not tokens. Defaulting to 3B as a rough
# Max 20x ballpark; override via env once real data firms up. Displayed
# with an "(est)" marker in /usage output so it never reads as official.
USAGE_WEEKLY_TOKEN_CAP = _env_int("USAGE_WEEKLY_TOKEN_CAP", "3000000000", min_value=1)

# --- Telegram constants ---
TELEGRAM_MSG_LIMIT = 4096
# Telegram clears the typing indicator ~5s after the last sendChatAction.
# A 2s refresh keeps it continuously visible during normal operation and
# gives ~3-5s as a "deadman's switch" when the bridge dies mid-turn —
# the indicator fades quickly enough to signal that no reply is coming.
TYPING_INTERVAL = 2  # seconds between typing indicators
SEND_RETRY_ATTEMPTS = 3
SEND_RETRY_BASE_DELAY = 1.0  # seconds; doubles each retry
MAX_QUEUED_MESSAGES = 20  # max pending messages per session before dropping

# --- Stall detection ---
# Claude CLI is API-bound, so CPU hovers near zero during normal operation.
# Stall detection watches stdout-event cadence, not CPU: claude -p in JSON
# output mode streams events on every tool call / assistant chunk / result,
# so a real hang (or a process blocked on a TCC dialog with no one to click
# it) shows up as no-events-for-N-minutes regardless of CPU. Default 10 min.
STALL_POLL_INTERVAL = 60  # check every minute
STALL_TIMEOUT = _env_int("STALL_TIMEOUT", "600", min_value=1)  # 10 min

# --- Shutdown ---
SHUTDOWN_PROCESS_TIMEOUT = 30  # seconds to wait for active processes

# --- Sentinel prefix for quota errors ---
QUOTA_HIT_PREFIX = "\x00QUOTA_HIT\x00"

# --- Regex patterns ---
SESSION_KEY_RE = re.compile(r"^[-\w]+$")
ANSI_RE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|\].*?(?:\x07|\x1b\\))")

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bridge")


# --- Persistence helpers ---
QUARANTINE_DIR = BASE_DIR / ".quarantine"


def atomic_write_text(path: Path, data: str, mode: int = 0o600) -> None:
    """Atomically write text to path via temp-file + rename.

    Prevents partial writes from poisoning JSON state when the process
    crashes or the disk fills mid-write. On success the destination has
    the given mode; on failure the destination is untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def quarantine_file(path: Path, reason: str) -> Path | None:
    """Move a corrupt state file aside so we stop tripping on it.

    The self-healing principle: we never silently unlink user-reachable
    data, we move it to a sibling .quarantine/ dir next to the original
    file with a timestamp so it can be inspected later. Using a sibling
    (rather than one global dir) keeps tests' tmp paths isolated.
    Returns the new path, or None if the move failed.
    """
    if not path.exists():
        return None
    try:
        quarantine_dir = path.parent / ".quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        import time as _t

        dest = quarantine_dir / f"{path.name}.{int(_t.time())}.bad"
        os.replace(path, dest)
        logger.warning("Quarantined %s -> %s (%s)", path, dest, reason)
        return dest
    except OSError as e:
        logger.error("Failed to quarantine %s: %s", path, e)
        return None


def safe_load_json(path: Path, expected_keys: tuple[str, ...] = ()) -> dict | list | None:
    """Load JSON, quarantining the file on corruption or schema mismatch.

    Returns the parsed object, or None if the file is missing / invalid
    (in which case the bad file has been moved to .quarantine/).
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        quarantine_file(path, f"unreadable: {e}")
        return None
    if expected_keys and isinstance(data, dict):
        missing = [k for k in expected_keys if k not in data]
        if missing:
            quarantine_file(path, f"missing keys: {missing}")
            return None
    return data
