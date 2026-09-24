"""Small utilities safe to import without starting the Telegram bridge."""

import logging
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
logger = logging.getLogger("bridge")


def load_bot_token() -> str:
    """Read the shared bot token from password-store, then the local environment."""
    load_dotenv(BASE_DIR / ".env")
    try:
        result = subprocess.run(
            ["pass", "show", "telegram-bot-token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def atomic_write_text(path: Path, data: str, mode: int = 0o600) -> None:
    """Atomically write text with private permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
