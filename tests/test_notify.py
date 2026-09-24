"""Standalone Telegram notification delivery."""

import io
import json
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest

from patchbay import notify, outbound


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_cli_delivers_topic_message_and_logs_after_success(monkeypatch, tmp_path):
    requests = []
    monkeypatch.setattr(outbound, "OUTBOUND_DIR", tmp_path)
    monkeypatch.setattr(notify, "load_bot_token", lambda: "secret")

    def send(request, timeout):
        requests.append((request, timeout))
        assert not (tmp_path / "-1003707564014_633.jsonl").exists()
        return Response()

    monkeypatch.setattr(notify, "urlopen", send)
    result = notify.main(
        ["--chat-id", "-1003707564014", "--thread-id", "633", "--source", "kimmy", "--stdin"],
        stdin=io.StringIO("hello"),
    )

    assert result == 0
    request, timeout = requests[0]
    assert request.full_url == "https://api.telegram.org/botsecret/sendMessage"
    assert timeout == 20
    assert parse_qs(request.data.decode()) == {
        "chat_id": ["-1003707564014"],
        "message_thread_id": ["633"],
        "text": ["hello"],
    }
    lines = (tmp_path / "-1003707564014_633.jsonl").read_text().splitlines()
    entry = json.loads(lines[0])
    assert entry["source"] == "kimmy"
    assert entry["text"] == "hello"


def test_cli_recovers_from_json_scalar_in_existing_log(monkeypatch, tmp_path, capsys):
    log_path = tmp_path / "-1001_633.jsonl"
    log_path.write_text('"malformed entry"\n')
    sends = []
    monkeypatch.setattr(outbound, "OUTBOUND_DIR", tmp_path)
    monkeypatch.setattr(notify, "load_bot_token", lambda: "secret")

    def send(request, timeout):
        sends.append(request)
        return Response()

    monkeypatch.setattr(notify, "urlopen", send)
    result = notify.main(
        ["--chat-id", "-1001", "--thread-id", "633", "--source", "kimmy", "--stdin"],
        stdin=io.StringIO("hello"),
    )
    output = capsys.readouterr()
    assert result == 0
    assert len(sends) == 1
    assert output.err == ""
    assert [json.loads(line)["text"] for line in log_path.read_text().splitlines()] == ["hello"]


def test_cli_exits_zero_with_fixed_warning_after_post_send_log_failure(monkeypatch, capsys):
    sends = []
    monkeypatch.setattr(notify, "load_bot_token", lambda: "secret")

    def send(request, timeout):
        sends.append(request)
        return Response()

    def fail_log(_session_key, _text, _source):
        raise AttributeError("secret in audit log exception")

    monkeypatch.setattr(notify, "urlopen", send)
    monkeypatch.setattr(notify, "log_outbound", fail_log)
    result = notify.main(
        ["--chat-id", "-1001", "--thread-id", "633", "--source", "kimmy", "--stdin"],
        stdin=io.StringIO("hello"),
    )
    output = capsys.readouterr()
    assert result == 0
    assert len(sends) == 1
    assert output.err.strip() == "DELIVERED_UNLOGGED"
    assert "secret" not in output.err


def test_cli_warns_when_outbound_write_fails_without_retrying(monkeypatch, tmp_path, capsys, caplog):
    sends = []
    monkeypatch.setattr(outbound, "OUTBOUND_DIR", tmp_path)
    monkeypatch.setattr(notify, "load_bot_token", lambda: "secret")

    def send(request, timeout):
        sends.append(request)
        return Response()

    def fail_write(_path, _text):
        raise OSError("secret in audit write failure")

    monkeypatch.setattr(notify, "urlopen", send)
    monkeypatch.setattr(outbound, "atomic_write_text", fail_write)
    result = notify.main(
        ["--chat-id", "-1001", "--thread-id", "633", "--source", "kimmy", "--stdin"],
        stdin=io.StringIO("hello"),
    )
    output = capsys.readouterr()
    assert result == 0
    assert len(sends) == 1
    assert output.err.strip() == "DELIVERED_UNLOGGED"
    assert "secret" not in output.err
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    ("chat_id", "thread_id", "text", "source"),
    [
        ("", "633", "hello", "kimmy"),
        ("-1001", "bad", "hello", "kimmy"),
        ("-1001", "633", "", "kimmy"),
        ("-1001", "633", "hello", ""),
        ("-1001", "633", "x" * 4097, "kimmy"),
    ],
    ids=["empty-chat", "bad-thread", "empty-text", "empty-source", "oversize"],
)
def test_validation_rejects_invalid_notification(chat_id, thread_id, text, source):
    with pytest.raises(ValueError, match="4096"):
        notify.validate_notification(chat_id, thread_id, text, source)


def test_validation_accepts_telegram_maximum():
    request = notify.validate_notification("-1001", "633", "x" * 4096, "kimmy")
    assert len(request.text) == 4096


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (HTTPError("https://example.invalid/token", 403, "secret rejected", {}, None), "TELEGRAM_HTTP_ERROR"),
        (URLError("secret network failure"), "TELEGRAM_NETWORK_ERROR"),
    ],
)
def test_failed_delivery_does_not_log_or_leak_error(monkeypatch, tmp_path, capsys, failure, expected_code):
    monkeypatch.setattr(outbound, "OUTBOUND_DIR", tmp_path)
    monkeypatch.setattr(notify, "load_bot_token", lambda: "secret")

    def fail(_request, timeout):
        raise failure

    monkeypatch.setattr(notify, "urlopen", fail)
    result = notify.main(
        ["--chat-id", "-1001", "--thread-id", "633", "--source", "kimmy", "--stdin"],
        stdin=io.StringIO("hello"),
    )
    output = capsys.readouterr()
    assert result == 1
    assert expected_code in output.err
    assert "secret" not in output.err
    assert not (tmp_path / "-1001_633.jsonl").exists()


def test_missing_token_is_diagnostic_and_does_not_send(monkeypatch, capsys):
    monkeypatch.setattr(notify, "load_bot_token", lambda: "")
    monkeypatch.setattr(notify, "urlopen", lambda *_args, **_kwargs: pytest.fail("should not send"))
    result = notify.main(
        ["--chat-id", "-1001", "--thread-id", "633", "--source", "kimmy", "--stdin"],
        stdin=io.StringIO("hello"),
    )
    assert result == 1
    assert "MISSING_BOT_TOKEN" in capsys.readouterr().err


def test_unexpected_transport_error_is_redacted(monkeypatch, capsys):
    monkeypatch.setattr(notify, "load_bot_token", lambda: "secret")

    def fail(_request, timeout):
        raise ValueError("secret from malformed URL")

    monkeypatch.setattr(notify, "urlopen", fail)
    result = notify.main(
        ["--chat-id", "-1001", "--thread-id", "633", "--source", "kimmy", "--stdin"],
        stdin=io.StringIO("hello"),
    )
    output = capsys.readouterr()
    assert result == 1
    assert "TELEGRAM_SEND_ERROR" in output.err
    assert "secret" not in output.err


def test_token_loader_error_is_redacted(monkeypatch, capsys):
    def fail():
        raise OSError("secret from credential helper")

    monkeypatch.setattr(notify, "load_bot_token", fail)
    result = notify.main(
        ["--chat-id", "-1001", "--thread-id", "633", "--source", "kimmy", "--stdin"],
        stdin=io.StringIO("hello"),
    )
    output = capsys.readouterr()
    assert result == 1
    assert "TOKEN_LOAD_ERROR" in output.err
    assert "secret" not in output.err


def test_import_is_independent_of_bridge_startup_configuration():
    environment = os.environ.copy()
    environment["CLAUDE_PATH"] = "/definitely/not/a/claude/binary"
    environment["CLAUDE_WORKING_DIR"] = "/definitely/not/a/working/directory"
    environment["PA_PLUGIN_DIR"] = "/definitely/not/a/plugin/directory"
    result = subprocess.run(
        [sys.executable, "-c", "import patchbay.notify; import patchbay.outbound"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
