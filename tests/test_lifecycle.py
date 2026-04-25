"""Tests for lifecycle functions: _graceful_shutdown, _get_proc_cpu, post_init, _stall_detector."""

import signal
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import bridge


# ── _get_proc_cpu ──────────────────────────────────────────────────────────


class TestGetProcCpu:
    def test_returns_float_for_running_process(self):
        with patch("bridge.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="12.5\n")
            result = bridge._get_proc_cpu(1234)
        assert result == 12.5

    def test_returns_none_for_dead_process(self):
        with patch("bridge.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = bridge._get_proc_cpu(99999)
        assert result is None

    def test_returns_none_on_timeout(self):
        with patch("bridge.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ps", timeout=5)
            result = bridge._get_proc_cpu(1234)
        assert result is None

    def test_returns_none_on_invalid_output(self):
        with patch("bridge.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not a number\n")
            result = bridge._get_proc_cpu(1234)
        assert result is None

    def test_returns_zero_cpu(self):
        with patch("bridge.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0.0\n")
            result = bridge._get_proc_cpu(1234)
        assert result == 0.0


# ── _graceful_shutdown ─────────────────────────────────────────────────────


class TestGracefulShutdown:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import stargate.sessions

        monkeypatch.setattr(stargate.sessions, "SESSION_DIR", tmp_path / "sessions")
        (tmp_path / "sessions").mkdir()
        monkeypatch.setattr(bridge, "PHOTO_DIR", tmp_path / "photos")
        (tmp_path / "photos").mkdir()
        monkeypatch.setattr(bridge, "_sessions", {})
        monkeypatch.setattr(bridge, "_remote_proc", None)
        bridge._shutting_down = False

    def test_sets_shutting_down_flag(self):
        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(signal.SIGTERM, None)
        assert bridge._shutting_down is True

    def test_terminates_active_procs(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = None
        bridge._get_session_state("key1").proc = proc
        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(signal.SIGTERM, None)
        proc.terminate.assert_called_once()

    def test_force_kills_on_timeout(self):
        proc = MagicMock()
        proc.poll.return_value = None
        # First wait (with timeout) raises TimeoutExpired; second wait (after kill) succeeds
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="", timeout=0),
            None,
        ]
        bridge._get_session_state("key1").proc = proc
        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(signal.SIGTERM, None)
        proc.kill.assert_called_once()

    def test_cleans_photo_dir(self, tmp_path):
        photo_dir = tmp_path / "photos"
        (photo_dir / "img1.jpg").write_text("fake")
        (photo_dir / "img2.jpg").write_text("fake")
        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(signal.SIGTERM, None)
        assert not list(photo_dir.glob("*.jpg"))

    def test_terminates_remote_proc(self, monkeypatch):
        remote = MagicMock()
        remote.poll.return_value = None
        remote.wait.return_value = None
        remote.pid = 12345
        monkeypatch.setattr(bridge, "_remote_proc", remote)
        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(signal.SIGTERM, None)
        remote.terminate.assert_called_once()

    def test_exits_with_zero(self):
        with pytest.raises(SystemExit) as exc_info:
            bridge._graceful_shutdown(signal.SIGTERM, None)
        assert exc_info.value.code == 0

    def test_handles_sigint(self):
        with pytest.raises(SystemExit):
            bridge._graceful_shutdown(signal.SIGINT, None)
        assert bridge._shutting_down is True


# ── _stall_detector ────────────────────────────────────────────────────────


class TestStallDetector:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setattr(bridge, "_sessions", {})
        monkeypatch.setattr(bridge, "_proc_last_active", {})
        monkeypatch.setattr(bridge, "_bot_instance", None)
        monkeypatch.setattr(bridge, "STALL_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(bridge, "STALL_TIMEOUT", 0.01)

    @pytest.mark.asyncio
    async def test_removes_finished_procs_from_tracking(self):
        """Finished processes should be removed from _proc_last_active."""
        proc = MagicMock()
        proc.poll.return_value = 0  # already finished
        bridge._get_session_state("done_key").proc = proc
        bridge._proc_last_active["done_key"] = 1000.0

        import asyncio

        task = asyncio.create_task(bridge._stall_detector())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert "done_key" not in bridge._proc_last_active

    @pytest.mark.asyncio
    async def test_kills_stalled_process(self):
        """A process idle beyond the timeout should be killed."""
        import time

        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 999
        bridge._get_session_state("stalled").proc = proc
        bridge._proc_last_active["stalled"] = time.time() - 1000  # way past timeout

        with patch.object(bridge, "_get_proc_cpu", return_value=0.0):
            import asyncio

            task = asyncio.create_task(bridge._stall_detector())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert proc.kill.call_count >= 1

    @pytest.mark.asyncio
    async def test_does_not_kill_active_process(self):
        """A process with high CPU should not be killed."""
        import time

        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 888
        bridge._get_session_state("active").proc = proc
        bridge._proc_last_active["active"] = time.time() - 1000

        with patch.object(bridge, "_get_proc_cpu", return_value=50.0):
            import asyncio

            task = asyncio.create_task(bridge._stall_detector())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        proc.kill.assert_not_called()


# ── _session_key ───────────────────────────────────────────────────────────


class TestSessionKey:
    def test_with_thread_id(self):
        assert bridge._session_key(123, 456) == "123_456"

    def test_without_thread_id(self):
        assert bridge._session_key(123, None) == "123"

    def test_negative_chat_id(self):
        assert bridge._session_key(-1003707564014, 327) == "-1003707564014_327"

    def test_zero_thread_id(self):
        assert bridge._session_key(123, 0) == "123_0"
