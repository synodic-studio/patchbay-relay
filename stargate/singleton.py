"""Single-instance guard for the Stargate bridge.

Without this, two bridge processes can poll Telegram's getUpdates
simultaneously during restart overlap. Telegram returns 409 Conflict to the
second, which logs it 1000+ times but never exits.

Self-healing: if the lockfile is held by a stale PID (process dead), we
steal it and continue. If held by a live PID, we wait up to
SINGLETON_WAIT_SECONDS for it to exit, then take over. Only if a live
process refuses to yield do we exit non-zero — launchd will retry, and by
then the other side should have released.

Usage:
    from stargate.singleton import acquire_singleton
    acquire_singleton()  # at the very top of main(), before anything else
"""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import sys
import time
from pathlib import Path

from .config import BASE_DIR, logger

LOCK_FILE = BASE_DIR / ".bridge.lock"
SINGLETON_WAIT_SECONDS = int(os.environ.get("SINGLETON_WAIT_SECONDS", "10"))

# Keep a module-level reference so the fd stays open for the process lifetime.
# When the process exits (any exit path), the kernel releases the flock.
_lock_fd: int | None = None


def _read_pid(path: Path) -> int | None:
    try:
        content = path.read_text().strip()
        return int(content) if content else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Return True if the given PID is an existing process we can signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else; treat as alive
    return True


def acquire_singleton() -> None:
    """Acquire the singleton lock or exit cleanly.

    Self-heals a stale lock (dead PID) automatically. If another live bridge
    holds the lock, waits briefly for it to exit, then either takes over or
    exits non-zero so launchd will retry.
    """
    global _lock_fd

    existing_pid = _read_pid(LOCK_FILE)
    if existing_pid is not None and _pid_alive(existing_pid):
        if existing_pid == os.getpid():
            # Paranoia: we're already us somehow. Proceed.
            logger.info("Singleton: reusing own lock (pid %d)", existing_pid)
        else:
            logger.warning(
                "Singleton: another bridge is alive (pid %d). Waiting up to %ds for it to exit.",
                existing_pid,
                SINGLETON_WAIT_SECONDS,
            )
            deadline = time.time() + SINGLETON_WAIT_SECONDS
            while time.time() < deadline and _pid_alive(existing_pid):
                time.sleep(0.5)
            if _pid_alive(existing_pid):
                logger.error(
                    "Singleton: pid %d still alive after %ds. Exiting so launchd can retry; "
                    "if this persists, SIGTERM pid %d manually.",
                    existing_pid,
                    SINGLETON_WAIT_SECONDS,
                    existing_pid,
                )
                # Exit 1 so launchd respawns us, by which time the other side
                # has usually released. Don't escalate to SIGKILL — could be a
                # legitimate manual run.
                sys.exit(1)
            logger.info("Singleton: pid %d exited; taking over the lock.", existing_pid)
    elif existing_pid is not None:
        logger.info("Singleton: stealing stale lock from dead pid %d", existing_pid)

    # Open (or create) the lockfile. O_CLOEXEC so child subprocesses don't
    # inherit the lock.
    try:
        fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as e:
        logger.error("Singleton: cannot open %s: %s", LOCK_FILE, e)
        sys.exit(1)

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EACCES, errno.EAGAIN):
            logger.error(
                "Singleton: flock busy on %s — another bridge has it. Exiting for launchd retry.",
                LOCK_FILE,
            )
            sys.exit(1)
        logger.error("Singleton: flock failed: %s", e)
        sys.exit(1)

    # Write our pid (truncate + write). Keep fd open.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    _lock_fd = fd
    logger.info("Singleton: acquired lock (pid %d) at %s", os.getpid(), LOCK_FILE)

    # Best-effort cleanup on normal exit. The kernel already releases flock
    # when the fd closes on process death, but unlinking the file removes
    # the visible stale-pid on clean exit.
    import atexit

    atexit.register(_release)


def _release() -> None:
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def release_singleton() -> None:
    """Public release (used by tests and explicit shutdown paths)."""
    _release()


# Convenience: try to signal the other bridge to exit, used by repair paths.
def signal_other_bridge(sig: int = signal.SIGTERM) -> int | None:
    """Send `sig` to the PID recorded in the lockfile, if any and alive.
    Returns the PID signalled, or None if nothing to signal.
    """
    pid = _read_pid(LOCK_FILE)
    if pid is None or pid == os.getpid() or not _pid_alive(pid):
        return None
    try:
        os.kill(pid, sig)
        return pid
    except OSError:
        return None
