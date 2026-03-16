"""Tests for stargate.config._SecretStr and other config edge cases."""


from stargate.config import _SecretStr


class TestSecretStr:
    def test_repr_redacted(self):
        s = _SecretStr("my-token-123")
        assert repr(s) == "***REDACTED***"

    def test_str_redacted(self):
        s = _SecretStr("my-token-123")
        assert str(s) == "***REDACTED***"

    def test_bool_true_for_nonempty(self):
        s = _SecretStr("value")
        assert bool(s) is True

    def test_bool_false_for_empty(self):
        s = _SecretStr("")
        assert bool(s) is False

    def test_reveal_returns_raw(self):
        s = _SecretStr("secret-value")
        assert s.reveal() == "secret-value"

    def test_not_in_fstring(self):
        s = _SecretStr("hunter2")
        assert "hunter2" not in f"Token is {s}"
        assert "REDACTED" in f"Token is {s}"
