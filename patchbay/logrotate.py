"""Startup log rotation for launchd-managed log files.

Launchd redirects the bridge's stderr to ``logs/bridge.err`` and doesn't
rotate. Historically the file grew unbounded. This module rotates the
file on bridge startup (after a crash or manual restart) and re-points
``sys.stderr`` at the freshly created path so the process keeps logging
somewhere sensible for the rest of its lifetime.

Rotation strategy (intentionally simple, no external deps):

  bridge.err            — active file
  bridge.err.1          — most-recent rotated copy (uncompressed for quick tail)
  bridge.err.2.gz .. .N — gzipped history, oldest dropped after ``keep``

``rotate_if_oversize`` is safe to call when the file is missing or
unreadable — it logs a warning and does nothing rather than crashing the
startup path.
"""

import gzip
import os
import shutil
import sys
from pathlib import Path

from .config import logger

# Defaults — small enough to keep disk use bounded, large enough that a
# routine restart doesn't churn rotations on every crash.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_KEEP = 5


def rotate_if_oversize(
    path: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> bool:
    """Rotate ``path`` if it exists and exceeds ``max_bytes``.

    Returns True if a rotation occurred. All errors are logged at warning
    level and suppressed — log rotation must never prevent the bridge from
    starting.
    """
    try:
        if not path.exists():
            return False
        size = path.stat().st_size
    except OSError as exc:
        logger.warning("Could not stat %s for rotation: %s", path, exc)
        return False

    if size < max_bytes:
        return False

    try:
        _shift_archives(path, keep=keep)
        # Rename the current log out of the way. A new file will be created
        # by reopen_fd() (or lazily by the next writer).
        rotated = path.with_suffix(path.suffix + ".1")
        os.replace(path, rotated)
        logger.info("Rotated %s (%d bytes) -> %s", path, size, rotated)
        return True
    except OSError as exc:
        logger.warning("Log rotation failed for %s: %s", path, exc)
        return False


def _shift_archives(path: Path, keep: int) -> None:
    """Rename existing .N / .N.gz archives up one slot, dropping the oldest.

    We compress on the transition from .1 (plain) to .2.gz so a recent
    rotated log stays easy to ``tail``, while older history saves disk.
    """
    # Drop the oldest if we already have `keep` rotated copies.
    oldest_gz = path.with_suffix(path.suffix + f".{keep}.gz")
    if oldest_gz.exists():
        try:
            oldest_gz.unlink()
        except OSError as exc:
            logger.warning("Could not unlink oldest rotation %s: %s", oldest_gz, exc)

    # Shift .2.gz → .3.gz, .3.gz → .4.gz, ...
    for n in range(keep - 1, 1, -1):
        src = path.with_suffix(path.suffix + f".{n}.gz")
        dst = path.with_suffix(path.suffix + f".{n + 1}.gz")
        if src.exists():
            try:
                os.replace(src, dst)
            except OSError as exc:
                logger.warning("Could not shift %s -> %s: %s", src, dst, exc)

    # Compress .1 → .2.gz if present.
    plain = path.with_suffix(path.suffix + ".1")
    if plain.exists():
        target = path.with_suffix(path.suffix + ".2.gz")
        try:
            with open(plain, "rb") as src_f, gzip.open(target, "wb") as gz_f:
                shutil.copyfileobj(src_f, gz_f)
            plain.unlink()
        except OSError as exc:
            logger.warning("Could not gzip %s: %s", plain, exc)


def reopen_std_stream(stream_name: str, path: Path) -> bool:
    """Re-point ``sys.stdout`` or ``sys.stderr`` at ``path`` (opened append).

    Launchd sets fd 1/2 to the log files before ``exec``. After we rotate
    the file, writes continue to the renamed inode until we reassign the
    fd — this function is that reassignment. Returns True on success; on
    failure the original stream is untouched and the error is logged.
    """
    assert stream_name in ("stdout", "stderr")
    target_fd = 1 if stream_name == "stdout" else 2
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(fd, target_fd)
        os.close(fd)
        # Make the Python stream write unbuffered to the new fd.
        new_stream = os.fdopen(target_fd, "a", buffering=1, closefd=False)
        setattr(sys, stream_name, new_stream)
        return True
    except OSError as exc:
        logger.warning("Could not reopen %s to %s: %s", stream_name, path, exc)
        return False


def rotate_startup_logs(
    log_dir: Path,
    names: list[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> None:
    """Rotate the standard bridge log files and reopen stderr/stdout.

    Call this once at bridge startup, before any significant logging. It
    never raises — a rotation failure is logged and startup continues so
    log hygiene never takes the bridge down.
    """
    names = names or ["bridge.err", "bridge.log"]
    for name in names:
        rotate_if_oversize(log_dir / name, max_bytes=max_bytes, keep=keep)
    # The two important reopens: stderr is where launchd sends tracebacks;
    # stdout carries logger output (bridge.log).
    reopen_std_stream("stderr", log_dir / "bridge.err")
    reopen_std_stream("stdout", log_dir / "bridge.log")
