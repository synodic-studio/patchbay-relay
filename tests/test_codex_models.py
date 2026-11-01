"""Discover the authenticated Codex model catalog through real fake processes."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap

import pytest

from patchbay.harness.codex_models import CodexModelsUnavailable, list_codex_models


def fake_server(tmp_path, body):
    path = tmp_path / "codex"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


SERVER = """
    capture = open(os.environ["CAPTURE_PATH"], "w")
    capture.write(json.dumps({"args":sys.argv[1:], "api": "OPENAI_API_KEY" in os.environ or "CODEX_API_KEY" in os.environ}) + "\\n")
    capture.flush()
    for line in sys.stdin:
        request = json.loads(line)
        capture.write(json.dumps(request) + "\\n")
        capture.flush()
        method = request["method"]
        if method == "initialized":
            continue
        if method == "initialize":
            result = {"userAgent":"codex/test"}
        elif method == "account/read":
            result = {"account":{"type":"chatgpt", "planType":"plus"}, "requiresOpenaiAuth":True}
        elif method == "model/list":
            if request["params"].get("cursor"):
                result = {"data":[{"id":"internal-row-2", "model":"gpt-visible", "displayName":"Duplicate"},
                                  {"id":"internal-row-3", "model":"gpt-other", "displayName":"Other Model"}], "nextCursor":None}
            else:
                result = {"data":[{"id":"internal-row-1", "model":"gpt-visible", "displayName":"Visible Model", "hidden":False},
                                  {"id":"hidden-row", "model":"gpt-hidden", "displayName":"Hidden", "hidden":True},
                                  {"id":"long-row", "model":"x"*53, "displayName":"Too long"},
                                  {"id":"unicode-row", "model":"é"*27, "displayName":"Too many bytes"}], "nextCursor":"page2"}
        else:
            raise AssertionError(method)
        print(json.dumps({"method":"notice", "params":{}}), flush=True)
        print(json.dumps({"id":request["id"], "result":result}), flush=True)
"""


def test_authenticated_catalog_uses_model_slug_filters_and_paginates(tmp_path, monkeypatch):
    capture = tmp_path / "capture.jsonl"
    monkeypatch.setenv("CAPTURE_PATH", str(capture))
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-use")
    monkeypatch.setenv("CODEX_API_KEY", "not-for-use")
    result = list_codex_models(codex_path=fake_server(tmp_path, SERVER))
    assert result == [("gpt-visible", "Visible Model"), ("gpt-other", "Other Model")]
    captured = [json.loads(line) for line in capture.read_text().splitlines()]
    assert not captured[0]["api"]
    args = captured[0]["args"]
    assert "app-server" in args and 'forced_login_method="chatgpt"' in args and 'model_provider="openai"' in args
    requests = captured[1:]
    assert [request["method"] for request in requests] == [
        "initialize",
        "initialized",
        "account/read",
        "model/list",
        "model/list",
    ]
    assert requests[2]["params"] == {"refreshToken": False}
    assert requests[3]["params"]["includeHidden"] is False
    assert requests[4]["params"]["cursor"] == "page2"


@pytest.mark.parametrize("account", [None, {"type": "apiKey"}, {"type": "amazonBedrock"}])
def test_non_subscription_auth_never_lists_models(tmp_path, monkeypatch, account):
    capture = tmp_path / "capture.jsonl"
    monkeypatch.setenv("CAPTURE_PATH", str(capture))
    body = SERVER.replace('{"type":"chatgpt", "planType":"plus"}', repr(account))
    with pytest.raises(CodexModelsUnavailable, match="ChatGPT"):
        list_codex_models(codex_path=fake_server(tmp_path, body))
    requests = [json.loads(line) for line in capture.read_text().splitlines()[1:]]
    assert not any(request["method"] == "model/list" for request in requests)


@pytest.mark.parametrize(
    "response",
    [
        '{"error":{"code":-32603,"message":"private sensitive detail"}}',
        '{"result":{"data":[],"nextCursor":null}}',
        '{"result":{"data":"malformed","nextCursor":null}}',
        '{"result":{"data":[{"model":"only-hidden", "hidden":true}],"nextCursor":null}}',
    ],
)
def test_catalog_failure_is_friendly_without_guessed_fallback(tmp_path, monkeypatch, response):
    monkeypatch.setenv("CAPTURE_PATH", str(tmp_path / "capture"))
    body = SERVER.replace(
        'print(json.dumps({"id":request["id"], "result":result}), flush=True)',
        f'print(json.dumps(dict(json.loads({response!r}), id=request["id"])) if method == "model/list" else json.dumps({{"id":request["id"], "result":result}}), flush=True)',
    )
    with pytest.raises(CodexModelsUnavailable) as error:
        list_codex_models(codex_path=fake_server(tmp_path, body))
    assert "private sensitive detail" not in str(error.value)


def test_timeout_kills_server_and_reaps_process(tmp_path):
    pidfile = tmp_path / "pid"
    body = f'open({str(pidfile)!r}, "w").write(str(os.getpid())); time.sleep(30)'
    with pytest.raises(CodexModelsUnavailable, match="timed out"):
        list_codex_models(codex_path=fake_server(tmp_path, body), timeout_seconds=0.5)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_missing_cli_is_friendly(tmp_path):
    with pytest.raises(CodexModelsUnavailable, match="Codex"):
        list_codex_models(codex_path=str(tmp_path / "absent"))
