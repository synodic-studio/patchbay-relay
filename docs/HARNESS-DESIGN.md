# Harness Design — Stargate

The seam that lets stargate run multiple coding-agent backends (Claude
Code CLI, Claude Agent SDK, codex/pi, cursor, …) behind one interface.

## Status

| Phase | Status | What landed |
|---|---|---|
| 0 — Design | ✅ | This doc + 4 open questions resolved |
| 1a — Protocol + CLI harness | ✅ | `stargate/harness/{base,claude_cli}.py`, 16 tests. Dormant — bridge unchanged. |
| 2 — SDK harness | ✅ | `stargate/harness/claude_sdk.py`, 24 tests. Dormant. Validated the protocol from a 2nd angle without any changes. |
| 1b — Rewire bridge (core) | ✅ | `bridge.run_claude` now drives `ClaudeCliHarness` via `_drive_harness_sync`. Popen/drain/parse moved out of the bridge. `proc_setter` callback mirrors the running proc into `SessionState.proc` so `/kill`, the stall detector, and graceful shutdown still work. `on_progress` keeps `state.last_event_at` fresh. 559 tests pass. |
| 1c — Per-chat selection + activity field | next | `chat_projects.json.harness` field + `STARGATE_DEFAULT_HARNESS` env + `/harness` command. Add `harness=` to every `activity.jsonl` entry that touches a turn. |
| 3 — Live soak | future | One topic on `cc-sdk` for as long as it takes to be boring. Compare via `harness=` field on `activity.jsonl`. |
| 4 — Flip default | future | `STARGATE_DEFAULT_HARNESS=cc-sdk`. Keep `cc-cli` as fallback. |
| 5 — Other backends | future | codex/pi, cursor — exercises `HarnessCapabilities.supports_resume=False`. |

## What 1b core delivered

- `bridge.run_claude` is now ~190 lines (was ~280). The cmd-construction,
  Popen, drain, and parse logic is gone — it lives in `ClaudeCliHarness`.
- `bridge._read_proc_streaming` deleted (replaced by
  `ClaudeCliHarness._drain_streams`). The stall detector still reads
  `SessionState.last_event_at`; the harness's `on_progress` callback
  refreshes it on each stdout line, same as before.
- `ClaudeCliHarness` gained a `proc_setter: Callable[[Popen | None], None]`
  param so the bridge can mirror the active subprocess into
  `SessionState.proc` for `/kill`, `_iter_active_procs`, the stall
  detector, and graceful shutdown.
- `TurnError.kind` is mapped one-to-one to the bridge's recovery
  branches:
  - `timeout` → "Timed out after N min" message
  - `corrupt_session` → `clear_session()` + recursive retry without resume
  - `oom` → `dispatch_repair("claude_oom_137")` + retry with trimmed prompt
  - `rate_limit` → `QUOTA_HIT_PREFIX + message` (Forge handoff)
  - `max_turns` → save session_id + return harness-built notice text
  - `unknown` / `process_died` → surface message verbatim
- Test fixtures updated to patch
  `stargate.harness.claude_cli.ClaudeCliHarness._drain_streams` instead
  of the removed `bridge._read_proc_streaming`. The
  `proc.communicate(timeout=...)` mocking pattern still works.

## What's left for 1c

1. `chat_projects.json` gains an optional `"harness": "cc-cli" | "cc-sdk"`
   field; default from env `STARGATE_DEFAULT_HARNESS=cc-cli`. New
   `/harness <name>` command surfaces and toggles the per-topic value.
2. Add `harness=` to every `activity.jsonl` entry that touches a turn so
   live-soak comparisons (phase 3) can grep `harness=cc-sdk` vs
   `harness=cc-cli` for behavioural diffs.

After 1c ships, phase 3 is just flipping one topic to `cc-sdk` and
watching.

## Open questions — resolved

| Q | Answer |
|---|---|
| Does SDK share CLI session storage? | Yes. Both use `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`. Cross-resume works. |
| How are quota errors surfaced? | SDK has typed `AssistantMessage.error == "rate_limit"`. Backstop on `ProcessError.stderr` + `ResultMessage.errors` for non-typed paths. |
| SDK failure isolation? | SDK runs CLI as subprocess. Same blast radius as today. OOM kills the child. |
| Capability gaps for non-Claude backends? | `HarnessCapabilities` flags. `supports_resume=False` is the main one for codex/cursor. |

## Risks for the remaining phases

| Risk | Mitigation |
|---|---|
| Phase 1b regresses the live bridge | All 560 tests must pass on the rewire commit. The chaos test (`test_chaos_run_claude.py`) covers the failure-mode matrix. Stage as a single commit so revert is one step. |
| Self-heal regressions on cc-sdk | Phase 3 soak. `harness=` field in `activity.jsonl` lets us grep behavior diffs. |
| Plugin-dir / MCP pass-through on SDK | `extra_args={"plugin-dir": ...}` confirmed working in phase 2 tests. MCP server config still needs a live test in phase 3. |
| Codex/cursor protocol mismatch | `HarnessCapabilities` already supports `supports_resume=False`. If a future backend can't fit the `TurnEvent` shape, we'll know in phase 5 — protocol revision is allowed at that point. |

## Reference: protocol contract

A `Harness.run_turn(req)` is an async iterator yielding `TurnEvent`s.
The stream **always ends with exactly one `TurnFinal` or `TurnError`**.
Any number of `TextDelta` / `ToolUse` / `ToolResult` events may precede
the terminator.

Source of truth: `stargate/harness/base.py`. Both harnesses verify the
contract via the test suites in `tests/test_harness_contract.py` and
`tests/test_harness_sdk.py`.

## Reference: CLI flag → SDK option mapping

Implemented in `ClaudeSdkHarness._build_options`. Recorded here for
non-Claude harnesses to learn from.

| CLI flag | SDK option |
|---|---|
| `--resume <id>` | `resume="<id>"` |
| `--max-turns N` | `max_turns=N` |
| `--allowed-tools` | `allowed_tools=[...]` |
| `--disallowed-tools` | `disallowed_tools=[...]` |
| `--model X` | `model="X"` |
| `--effort X` | `effort="X"` |
| `--plugin-dir P` | `extra_args={"plugin-dir": P}` |
| `--append-system-prompt S` | `system_prompt={"type": "preset", "preset": "claude_code", "append": S}` |
| `--dangerously-skip-permissions` | `permission_mode="bypassPermissions"` |

## Reference: TurnError.kind → self-heal mapping

Implemented in `stargate/self_heal.py`. Phase 1b adds the new kinds.

| `TurnError.kind` | Existing handler | Notes |
|---|---|---|
| `oom` | `claude_oom_137` | Retry with trimmed prompt + smaller turn budget |
| `corrupt_session` | `corrupt_session_json` | Quarantine + retry fresh session |
| `rate_limit` | (Forge handoff) | `_maybe_handoff_quota` in bridge |
| `max_turns` | (none — surface notice) | Append turn-limit message |
| `timeout` | (none — surface notice) | "Timed out after N min" |
| `process_died` | new in 1b | `harness_subprocess_died` — surface stderr, no retry |
