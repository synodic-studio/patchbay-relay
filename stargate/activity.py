"""Activity logging — structured JSON-lines event log."""

import json
import time

from .config import ACTIVITY_LOG, logger


def log_activity(event: str, **kwargs) -> None:
    """Append a structured JSON-lines entry to the activity log."""
    entry = {"ts": time.time(), "event": event, **kwargs}
    try:
        with open(ACTIVITY_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        logger.debug("Failed to write activity log")
