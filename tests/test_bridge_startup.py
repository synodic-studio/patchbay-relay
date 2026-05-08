"""Tests for bridge.py startup validation and core helpers."""

import os
import subprocess
import sys
from pathlib import Path

BRIDGE_DIR = Path(__file__).parent.parent


def _import_bridge(env_overrides: dict) -> subprocess.CompletedProcess:
    """Import bridge.py in a subprocess with the given environment overrides.

    pass (password-store) may be unavailable in CI, so TELEGRAM_BOT_TOKEN is
    provided directly to bypass the secret lookup and the BOT_TOKEN check.
    """
    env = {
        **os.environ,
        "TELEGRAM_BOT_TOKEN": "fake_token_for_tests",
        **env_overrides,
    }
    return subprocess.run(
        [sys.executable, "-c", "import bridge"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(BRIDGE_DIR),
    )


class TestAllowedUserIdsParsing:
    def test_valid_single_id(self):
        result = _import_bridge({"ALLOWED_USER_IDS": "123456789"})
        assert result.returncode == 0, result.stderr

    def test_valid_multiple_ids(self):
        result = _import_bridge({"ALLOWED_USER_IDS": "123456789,987654321"})
        assert result.returncode == 0, result.stderr

    def test_empty_value_is_allowed(self):
        result = _import_bridge({"ALLOWED_USER_IDS": ""})
        assert result.returncode == 0, result.stderr

    def test_whitespace_around_ids(self):
        result = _import_bridge({"ALLOWED_USER_IDS": " 123 , 456 "})
        assert result.returncode == 0, result.stderr

    def test_non_integer_rejects_with_exit_1(self):
        result = _import_bridge({"ALLOWED_USER_IDS": "123,not_a_number"})
        assert result.returncode == 1

    def test_non_integer_error_message(self):
        result = _import_bridge({"ALLOWED_USER_IDS": "123,not_a_number"})
        assert "ALLOWED_USER_IDS" in result.stderr
        assert "not_a_number" in result.stderr

    def test_float_rejects(self):
        result = _import_bridge({"ALLOWED_USER_IDS": "123,45.6"})
        assert result.returncode == 1
        assert "45.6" in result.stderr

    def test_leading_non_integer_rejects(self):
        result = _import_bridge({"ALLOWED_USER_IDS": "abc,123"})
        assert result.returncode == 1
        assert "abc" in result.stderr

    def test_trailing_comma_ignored(self):
        """Trailing comma produces an empty token, which should be silently skipped."""
        result = _import_bridge({"ALLOWED_USER_IDS": "123,"})
        assert result.returncode == 0, result.stderr
