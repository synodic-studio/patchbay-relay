"""Tests for env-var validation helpers in stargate.config (CTB-apy).

These guard the startup path: a bad .env entry should exit with a clear
error on stderr, not an unhelpful ValueError traceback.
"""

import os

import pytest

from stargate import config


class TestEnvInt:
    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("STARGATE_TEST_X", raising=False)
        assert config._env_int("STARGATE_TEST_X", "42") == 42

    def test_returns_env_value_when_set(self, monkeypatch):
        monkeypatch.setenv("STARGATE_TEST_X", "99")
        assert config._env_int("STARGATE_TEST_X", "42") == 99

    def test_non_integer_fatal_exits(self, monkeypatch, capsys):
        monkeypatch.setenv("STARGATE_TEST_X", "abc")
        with pytest.raises(SystemExit) as exc:
            config._env_int("STARGATE_TEST_X", "42")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "STARGATE_TEST_X='abc'" in err
        assert "not an integer" in err

    def test_below_minimum_fatal_exits(self, monkeypatch, capsys):
        monkeypatch.setenv("STARGATE_TEST_X", "0")
        with pytest.raises(SystemExit):
            config._env_int("STARGATE_TEST_X", "1", min_value=1)
        assert "below the minimum" in capsys.readouterr().err


class TestEnvExistingPath:
    def test_returns_expanded_when_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STARGATE_TEST_PATH", str(tmp_path))
        assert config._env_existing_path("STARGATE_TEST_PATH", "/does/not/matter", "desc") == str(tmp_path)

    def test_missing_path_fatal_exits(self, monkeypatch, capsys):
        monkeypatch.setenv("STARGATE_TEST_PATH", "/definitely/not/a/real/path/xyz123")
        with pytest.raises(SystemExit) as exc:
            config._env_existing_path("STARGATE_TEST_PATH", "/unused", "widgets dir")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "widgets dir does not exist" in err

    def test_tilde_expansion(self, monkeypatch):
        # HOME is always a real dir; use it as a known-good target
        home = os.path.expanduser("~")
        monkeypatch.setenv("STARGATE_TEST_PATH", "~")
        assert config._env_existing_path("STARGATE_TEST_PATH", "/unused", "home") == home


class TestResolveClaudeBinary:
    def test_uses_configured_when_executable(self, tmp_path):
        binary = tmp_path / "claude"
        binary.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(binary, 0o755)
        assert config._resolve_claude_binary(str(binary)) == str(binary)

    def test_falls_back_to_canonical_path(self, tmp_path, monkeypatch, caplog):
        import logging

        fallback = tmp_path / "claude"
        fallback.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(fallback, 0o755)

        # Patch shutil.which to return our fake; patch os.path.isfile so the
        # hardcoded ~/.local/bin/claude etc. don't accidentally find a real one.
        def fake_isfile(p):
            return p == str(fallback)

        def fake_access(p, mode):
            return p == str(fallback)

        monkeypatch.setattr(config.os.path, "isfile", fake_isfile)
        monkeypatch.setattr(config.os, "access", fake_access)
        monkeypatch.setattr(config.shutil, "which", lambda _: str(fallback))

        with caplog.at_level(logging.WARNING, logger="bridge"):
            result = config._resolve_claude_binary("/bogus/path/to/claude")

        assert result == str(fallback)
        assert any("falling back" in r.message for r in caplog.records)

    def test_no_binary_found_fatal_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(config.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(config.shutil, "which", lambda _: None)
        with pytest.raises(SystemExit) as exc:
            config._resolve_claude_binary("/bogus/claude")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "claude CLI not found" in err
        assert "/bogus/claude" in err
