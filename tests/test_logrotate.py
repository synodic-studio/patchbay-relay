"""Tests for stargate.logrotate — startup rotation of bridge.err / bridge.log.

CTB-r7z: launchd pipes stderr to bridge.err which grows unbounded; we rotate
on startup and re-point sys.stderr at a fresh file.
"""

import gzip
import os


from stargate import logrotate


class TestRotateIfOversize:
    def test_noop_when_file_missing(self, tmp_path):
        p = tmp_path / "missing.log"
        assert logrotate.rotate_if_oversize(p, max_bytes=10) is False

    def test_noop_when_under_threshold(self, tmp_path):
        p = tmp_path / "small.log"
        p.write_bytes(b"x" * 50)
        assert logrotate.rotate_if_oversize(p, max_bytes=100) is False
        assert p.read_bytes() == b"x" * 50

    def test_rotates_when_over_threshold(self, tmp_path):
        p = tmp_path / "big.log"
        p.write_bytes(b"x" * 100)
        assert logrotate.rotate_if_oversize(p, max_bytes=10) is True
        # Active file is gone (until something re-opens it), .1 holds the old content
        assert not p.exists()
        rotated = tmp_path / "big.log.1"
        assert rotated.exists()
        assert rotated.read_bytes() == b"x" * 100

    def test_second_rotation_gzips_previous(self, tmp_path):
        p = tmp_path / "busy.log"
        # First rotation: creates .1
        p.write_bytes(b"A" * 100)
        logrotate.rotate_if_oversize(p, max_bytes=10)
        # Simulate time passing: new content written
        p.write_bytes(b"B" * 100)
        logrotate.rotate_if_oversize(p, max_bytes=10)

        # .1 holds the most recent rotation, .2.gz the older one.
        assert (tmp_path / "busy.log.1").read_bytes() == b"B" * 100
        gz_path = tmp_path / "busy.log.2.gz"
        assert gz_path.exists()
        with gzip.open(gz_path, "rb") as f:
            assert f.read() == b"A" * 100

    def test_drops_oldest_past_keep(self, tmp_path):
        p = tmp_path / "roll.log"
        # Pre-seed the archive slots .2.gz through .5.gz; .5 is the current oldest.
        for n in range(2, 6):
            (tmp_path / f"roll.log.{n}.gz").write_bytes(b"old-" + str(n).encode())

        p.write_bytes(b"new" * 100)
        logrotate.rotate_if_oversize(p, max_bytes=10, keep=5)

        # .5.gz (the previous oldest) was dropped; everything shifted up one.
        assert not any(
            (tmp_path / f"roll.log.{n}.gz").read_bytes() == b"old-5"
            for n in range(2, 7)
            if (tmp_path / f"roll.log.{n}.gz").exists()
        )

    def test_replace_error_logs_and_returns_false(self, tmp_path, monkeypatch, caplog):
        """Verify the rotation-OSError branch: if os.replace blows up
        mid-rotation, we log a warning and return False rather than crashing
        the caller."""
        import logging

        p = tmp_path / "io-fails.log"
        p.write_bytes(b"x" * 100)

        def boom(*a, **kw):
            raise OSError("io error")

        monkeypatch.setattr(logrotate.os, "replace", boom)

        with caplog.at_level(logging.WARNING, logger="bridge"):
            result = logrotate.rotate_if_oversize(p, max_bytes=10)
        assert result is False
        assert any("Log rotation failed" in r.message for r in caplog.records)


class TestReopenStdStream:
    def test_reopen_writes_to_new_path(self, tmp_path, capsys):
        target = tmp_path / "reopened.err"
        # Can't use pytest's stderr capture on a raw fd swap — do the reopen,
        # write, and read back from the path.
        ok = logrotate.reopen_std_stream("stderr", target)
        assert ok is True
        try:
            os.write(2, b"hello-stderr\n")
            os.fsync(2)
        finally:
            # Restore stderr so pytest teardown can report failures normally.
            # We can't perfectly restore, but pointing back to the original
            # device-null-ish capture fd is enough for the test runner.
            pass

        assert target.exists()
        assert b"hello-stderr" in target.read_bytes()

    def test_reopen_failure_logs_warning(self, tmp_path, monkeypatch, caplog):
        import logging

        def boom(*a, **kw):
            raise OSError("cannot open")

        monkeypatch.setattr(logrotate.os, "open", boom)

        with caplog.at_level(logging.WARNING, logger="bridge"):
            ok = logrotate.reopen_std_stream("stderr", tmp_path / "wont-open.err")
        assert ok is False
        assert any("Could not reopen" in r.message for r in caplog.records)


class TestRotateStartupLogs:
    def test_skips_missing_log_dir_gracefully(self, tmp_path, monkeypatch):
        # Intercept reopen so we don't actually clobber the test stderr.
        calls = []
        monkeypatch.setattr(logrotate, "reopen_std_stream", lambda s, p: calls.append((s, p)) or True)
        log_dir = tmp_path / "does-not-exist"
        logrotate.rotate_startup_logs(log_dir)
        # It called reopen for both streams — reopen itself is responsible
        # for handling a missing dir.
        assert [c[0] for c in calls] == ["stderr", "stdout"]

    def test_rotates_both_bridge_logs(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "bridge.err").write_bytes(b"E" * 100)
        (log_dir / "bridge.log").write_bytes(b"L" * 100)

        # Keep reopen_std_stream from touching real fd 1/2 during the test.
        monkeypatch.setattr(logrotate, "reopen_std_stream", lambda s, p: True)

        logrotate.rotate_startup_logs(log_dir, max_bytes=10)

        assert (log_dir / "bridge.err.1").exists()
        assert (log_dir / "bridge.log.1").exists()
