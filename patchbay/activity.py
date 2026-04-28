"""Activity logging — structured JSON-lines event log."""

import json
import time
from pathlib import Path

from .config import ACTIVITY_LOG, logger

# 10 MB rotation threshold
_MAX_LOG_SIZE = 10 * 1024 * 1024


def _rotate_if_needed() -> None:
    """If activity log exceeds 10 MB, rotate to .1 backup."""
    try:
        log_path = Path(ACTIVITY_LOG)
        if log_path.exists() and log_path.stat().st_size > _MAX_LOG_SIZE:
            backup = log_path.with_suffix(".jsonl.1")
            log_path.replace(backup)
            logger.info("Rotated activity log (backup: %s)", backup)
    except OSError:
        logger.debug("Failed to rotate activity log")


def log_activity(event: str, **kwargs) -> None:
    """Append a structured JSON-lines entry to the activity log."""
    _rotate_if_needed()
    entry = {"ts": time.time(), "event": event, **kwargs}
    try:
        with open(ACTIVITY_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        logger.debug("Failed to write activity log")
