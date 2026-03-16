"""Configuration: environment variables, constants, and paths.

All configuration is centralized here. Other modules import from this module
rather than reading os.environ directly.
"""

import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")


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
    """Load the Telegram bot token from pass-cli, keychain, or env var."""
    try:
        pp = subprocess.run(
            [
                "pass-cli",
                "item",
                "view",
                "--vault-name",
                "Developer Secrets",
                "--item-title",
                "telegram-bot-token",
                "--field",
                "note",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if pp.returncode == 0 and pp.stdout.strip():
            return _SecretStr(pp.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        kc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                "bryancostanza",
                "-s",
                "telegram-bot-token",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if kc.returncode == 0 and kc.stdout.strip():
            return _SecretStr(kc.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return _SecretStr(os.environ.get("TELEGRAM_BOT_TOKEN", ""))


def _load_allowed_user_ids() -> set[int]:
    """Parse ALLOWED_USER_IDS from env var. Exits on invalid input."""
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    if not raw.strip():
        return set()
    parsed: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            parsed.add(int(token))
        except ValueError:
            print(
                f"ERROR: ALLOWED_USER_IDS contains non-integer value: {token!r}. "
                "All entries must be numeric Telegram user IDs (e.g. 123456789,987654321).",
                file=sys.stderr,
            )
            sys.exit(1)
    return parsed


# --- Bot token ---
BOT_TOKEN = _load_bot_token()
if not BOT_TOKEN:
    print(
        "ERROR: BOT_TOKEN is empty. Set TELEGRAM_BOT_TOKEN in the environment "
        "or ensure 'telegram-bot-token' is accessible via Proton Pass or Keychain.",
        file=sys.stderr,
    )
    sys.exit(1)

# --- User allowlist ---
ALLOWED_USER_IDS = _load_allowed_user_ids()

# --- Paths ---
CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "/opt/homebrew/bin/claude")
WORKING_DIR = os.environ.get("CLAUDE_WORKING_DIR", os.path.expanduser("~/Developer"))
PA_PLUGIN_DIR = os.environ.get(
    "PA_PLUGIN_DIR",
    os.path.expanduser("~/Developer/Fanta"),
)

BASE_DIR = Path(__file__).parent.parent
SESSION_DIR = BASE_DIR / "sessions"
SESSION_DIR.mkdir(exist_ok=True)
PENDING_DIR = BASE_DIR / "pending"
PENDING_DIR.mkdir(exist_ok=True)
RESTART_NOTIFY_FILE = BASE_DIR / "restart_notify.json"
CHAT_PROJECTS_FILE = BASE_DIR / "chat_projects.json"
FORGE_QUEUE_DIR = Path(PA_PLUGIN_DIR) / "agents" / "dev" / "forge" / "queue"
ACTIVITY_LOG = BASE_DIR / "activity.jsonl"
PHOTO_DIR = Path(tempfile.gettempdir()) / "claude-telegram-photos"
PHOTO_DIR.mkdir(exist_ok=True)

# --- Tunables ---
SESSION_EXPIRY = int(os.environ.get("SESSION_EXPIRY", "259200"))  # 3 days
MAX_TIMEOUT = int(os.environ.get("MAX_TIMEOUT", "2700"))  # 45 min safety valve
MAX_TURNS = int(os.environ.get("MAX_TURNS", "30"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
AUTH_BASE_URL = os.environ.get("AUTH_BASE_URL", "https://auth.kj6.dev")
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "false").lower() == "true"

# --- Telegram constants ---
TELEGRAM_MSG_LIMIT = 4096
TYPING_INTERVAL = 4  # seconds between typing indicators
SEND_RETRY_ATTEMPTS = 3
SEND_RETRY_BASE_DELAY = 1.0  # seconds; doubles each retry
MAX_QUEUED_MESSAGES = 20  # max pending messages per session before dropping

# --- Stall detection ---
STALL_POLL_INTERVAL = 120  # check every 2 minutes
STALL_CPU_THRESHOLD = 1.0  # %CPU below this = idle
STALL_TIMEOUT = 600  # kill after 10 min of near-zero CPU

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
