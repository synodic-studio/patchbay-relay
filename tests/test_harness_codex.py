"""Codex CLI contract tests using real subprocesses and recorded JSONL shapes."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from patchbay.harness.base import Harness, TextDelta, ToolResult, ToolUse, TurnError, TurnFinal, TurnRequest
from patchbay.harness.codex import CodexHarness


def fake_codex(tmp_path: Path, body: str) -> str:
    path = tmp_path / "codex"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def request(tmp_path: Path, **kwargs) -> TurnRequest:
    args = dict(
        prompt="hello",
        session_key="test",
        project_dir=tmp_path,
        system_prompt="",
        resume_session_id=None,
        model=None,
        effort=None,
        allowed_tools=None,
        disallowed_tools=None,
        max_turns=None,
        plugin_dir=None,
    )
    args.update(kwargs)
    return TurnRequest(**args)


async def collect(harness, req):
    return [event async for event in harness.run_turn(req)]


@pytest.mark.parametrize("resume", [None, "a86537ce-aa36-49d8-b6d2-81b092ee2d78"])
async def test_subprocess_input_subscription_environment_and_resume(tmp_path, resume, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "api-must-not-be-used")
    path = fake_codex(
        tmp_path,
        """
        open("invocation.json", "w").write(json.dumps({"args":sys.argv[1:], "prompt":sys.stdin.read(),
            "has_api": "OPENAI_API_KEY" in os.environ or "CODEX_API_KEY" in os.environ}))
        print(json.dumps({"type":"thread.started", "thread_id":"thread-123"}))
        print(json.dumps({"type":"item.completed", "item":{"id":"item_0", "type":"agent_message", "text":"Hi"}}))
        print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":12,"output_tokens":2}}))
    """,
    )
    harness = CodexHarness(codex_path=path, env_overrides={"CODEX_API_KEY": "also-removed"})
    assert isinstance(harness, Harness)
    prompt = "A" * 200000
    events = await collect(
        harness,
        request(
            tmp_path,
            prompt=prompt,
            resume_session_id=resume,
            system_prompt='Rules\n"quoted"',
            model="gpt-5.4",
            effort="max",
        ),
    )
    invocation = json.loads((tmp_path / "invocation.json").read_text())
    args = invocation["args"]
    assert args[:2] == (["exec", "resume"] if resume else ["exec", "--json"])
    assert args[-1] == "-"
    assert (resume in args) if resume else "resume" not in args
    assert "--json" in args and "--skip-git-repo-check" in args
    assert 'forced_login_method="chatgpt"' in args
    assert 'approval_policy="never"' in args
    assert 'sandbox_mode="danger-full-access"' in args
    assert 'model_reasoning_effort="xhigh"' in args
    assert 'developer_instructions="Rules\\n\\"quoted\\""' in args
    assert args[args.index("--model") + 1] == "gpt-5.4"
    assert invocation["prompt"] == prompt and not invocation["has_api"]
    assert events[-1] == TurnFinal("thread-123", 1, None, "Hi")
    assert TextDelta("Hi", final=True) in events


async def test_tool_events_stream_before_process_exit(tmp_path):
    path = fake_codex(
        tmp_path,
        """
        sys.stdin.read()
        print(json.dumps({"type":"item.started", "item":{"id":"item_1", "type":"command_execution", "command":"pwd", "status":"in_progress"}}), flush=True)
        time.sleep(.3)
        print(json.dumps({"type":"item.completed", "item":{"id":"item_1", "type":"command_execution", "command":"pwd", "aggregated_output":"/project", "exit_code":0, "status":"completed"}}))
        print(json.dumps({"type":"item.started", "item":{"id":"item_2", "type":"mcp_tool_call", "server":"docs", "tool":"search", "arguments":{"query":"help"}}}))
        print(json.dumps({"type":"item.completed", "item":{"id":"item_2", "type":"mcp_tool_call", "server":"docs", "tool":"search", "result":{"text":"answer"}, "status":"completed"}}))
        print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"Done"}}))
        print(json.dumps({"type":"turn.completed"}))
    """,
    )
    processes, progress = [], []
    h = CodexHarness(codex_path=path, proc_setter=processes.append, on_progress=lambda: progress.append(True))
    stream = h.run_turn(request(tmp_path))
    first = await anext(stream)
    assert first == ToolUse("command_execution", {"command": "pwd"}, "item_1")
    assert processes[-1].poll() is None
    events = [first] + [e async for e in stream]
    assert ToolResult("item_1", "/project", False) in events
    assert ToolUse("docs.search", {"query": "help"}, "item_2") in events
    assert ToolResult("item_2", '{"text": "answer"}', False) in events
    assert processes[-1] is None and progress


@pytest.mark.parametrize(
    "body,kind",
    [
        ('print("broken", file=sys.stderr); sys.exit(3)', "process_died"),
        ('print(json.dumps({"type":"turn.completed"})); sys.exit(1)', "process_died"),
        (
            'print(json.dumps({"type":"item.completed", "item":{"type":"agent_message","text":"partial"}}))',
            "process_died",
        ),
        ('print(json.dumps({"type":"turn.failed", "error":{"message":"Usage limit reached"}}))', "rate_limit"),
        ('print(json.dumps({"type":"error", "message":"Too many requests (429)"}))', "rate_limit"),
        ('print("No session found with id old", file=sys.stderr); sys.exit(1)', "corrupt_session"),
        ("time.sleep(10)", "timeout"),
    ],
)
async def test_failure_never_emits_success(tmp_path, body, kind):
    h = CodexHarness(codex_path=fake_codex(tmp_path, body), max_timeout_seconds=0.2 if kind == "timeout" else 3)
    events = await collect(h, request(tmp_path, resume_session_id="old"))
    assert isinstance(events[-1], TurnError) and events[-1].kind == kind
    assert not any(isinstance(e, TurnFinal) for e in events)


async def test_missing_executable_is_classified(tmp_path):
    events = await collect(CodexHarness(codex_path=str(tmp_path / "missing")), request(tmp_path))
    assert len(events) == 1 and isinstance(events[0], TurnError)


async def test_cancel_terminates_child_and_clears_callback(tmp_path):
    path = fake_codex(
        tmp_path, 'print(json.dumps({"type":"thread.started","thread_id":"x"}), flush=True); time.sleep(10)'
    )
    procs = []
    h = CodexHarness(codex_path=path, proc_setter=procs.append)
    task = asyncio.create_task(collect(h, request(tmp_path)))
    for _ in range(100):
        if procs:
            break
        await asyncio.sleep(0.01)
    await h.cancel()
    events = await asyncio.wait_for(task, 2)
    assert isinstance(events[-1], TurnError)
    assert procs[0].poll() is not None and procs[-1] is None
    await h.cancel()


async def test_final_response_excludes_intermediate_commentary(tmp_path):
    path = fake_codex(
        tmp_path,
        """
        print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"Working on it"}}))
        print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"Finished"}}))
        print(json.dumps({"type":"turn.completed"}))
    """,
    )
    events = await collect(CodexHarness(codex_path=path), request(tmp_path))
    assert events[-1].raw_text == "Finished"


async def test_external_oom_kill_classified_separately_from_cancel(tmp_path):
    path = fake_codex(tmp_path, "os.kill(os.getpid(), 9)")
    events = await collect(CodexHarness(codex_path=path), request(tmp_path))
    assert isinstance(events[-1], TurnError) and events[-1].kind == "oom"


async def test_cancelling_consumer_task_reaps_process(tmp_path):
    procs = []
    path = fake_codex(tmp_path, "time.sleep(10)")
    h = CodexHarness(codex_path=path, proc_setter=procs.append)
    task = asyncio.create_task(collect(h, request(tmp_path)))
    for _ in range(100):
        if procs:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert procs[0].poll() is not None and procs[-1] is None


async def test_recovered_error_diagnostic_does_not_poison_success(tmp_path):
    path = fake_codex(
        tmp_path,
        """
        print(json.dumps({"type":"error", "message":"Reconnecting after rate limit"}))
        print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"Recovered"}}))
        print(json.dumps({"type":"turn.completed"}))
    """,
    )
    events = await collect(CodexHarness(codex_path=path), request(tmp_path))
    assert events[-1] == TurnFinal(None, 1, None, "Recovered")
    assert not any(isinstance(event, TurnError) for event in events)


@pytest.mark.parametrize("terminal", ["turn.failed", "turn.completed"])
async def test_terminal_failure_or_nonzero_exit_retains_error_diagnostic(tmp_path, terminal):
    path = fake_codex(
        tmp_path,
        f"""
        print(json.dumps({{"type":"error", "message":"Rate limit reached"}}))
        print(json.dumps({{"type":{terminal!r}}}))
        sys.exit({0 if terminal == "turn.failed" else 1})
    """,
    )
    events = await collect(CodexHarness(codex_path=path), request(tmp_path))
    assert isinstance(events[-1], TurnError) and events[-1].kind == "rate_limit"
    assert "Rate limit reached" in events[-1].message


async def test_turn_failed_remains_terminal_even_with_later_completion(tmp_path):
    path = fake_codex(
        tmp_path,
        """
        print(json.dumps({"type":"turn.failed", "error":{"message":"Model unavailable"}}))
        print(json.dumps({"type":"turn.completed"}))
    """,
    )
    events = await collect(CodexHarness(codex_path=path), request(tmp_path))
    assert isinstance(events[-1], TurnError)
    assert "Model unavailable" in events[-1].message


async def test_repeated_nested_model_error_is_readable_and_actionable(tmp_path):
    diagnostic = {
        "type": "error",
        "status": 400,
        "error": {
            "type": "invalid_request_error",
            "message": "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.",
            "code": "model_not_found",
        },
    }
    path = fake_codex(
        tmp_path,
        f"""
        diagnostic = {json.dumps(diagnostic)!r}
        print(json.dumps({{"type":"error", "message":diagnostic}}))
        print(json.dumps({{"type":"error", "message":diagnostic}}))
        print(json.dumps({{"type":"turn.failed", "error":{{"message":diagnostic}}}}))
        sys.exit(1)
    """,
    )
    events = await collect(CodexHarness(codex_path=path), request(tmp_path, model="gpt-6.1-sol"))
    error = events[-1]
    assert isinstance(error, TurnError) and error.kind == "process_died"
    assert "/model" in error.message and "subscription login" in error.message
    assert "{" not in error.message and '\\"' not in error.message
    assert len(error.message.splitlines()) == 1
    assert error.message.count("/model") == 1
    assert not error.retryable


async def test_nested_non_model_errors_keep_message_code_and_classification(tmp_path):
    diagnostic = json.dumps(
        {"error": {"message": json.dumps({"message": "Please retry later", "code": "rate_limit_exceeded"})}}
    )
    path = fake_codex(
        tmp_path,
        f"""
        print(json.dumps({{"type":"error", "message":{diagnostic!r}}}))
        print(json.dumps({{"type":"turn.failed", "error":{{"message":{diagnostic!r}}}}}))
    """,
    )
    events = await collect(CodexHarness(codex_path=path), request(tmp_path))
    assert isinstance(events[-1], TurnError) and events[-1].kind == "rate_limit"
    assert events[-1].message == "Please retry later (rate_limit_exceeded)"
