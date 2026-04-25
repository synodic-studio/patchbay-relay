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
| 1c — Per-chat selection + activity field | ✅ | `chat_projects.json.harness` field + `STARGATE_DEFAULT_HARNESS` env + `/harness` command. Every `activity.jsonl` entry that touches a turn carries `harness=<effective>` and `harness_requested=<requested>`. 573 tests. |
| 3a — Backend-agnostic cancel | ✅ | `SessionState.harness` + `worker_loop` fields. `_cancel_session_async` dispatches `proc.kill()` for cc-cli or `run_coroutine_threadsafe(harness.cancel(), worker_loop)` for cc-sdk. `_iter_active_sessions` is the new backend-agnostic snapshot used by /ping, the stall detector, /restart, shutdown. 582 tests. |
| 3b — cc-sdk dispatch | ✅ | `run_claude` instantiates `ClaudeSdkHarness` when cc-sdk is selected. SDK harness gained `on_progress` so `last_event_at` refreshes per SDK message. /kill, stall detector, shutdown all route cancellation through the unified helper. 583 tests. |
| 3c — Soak tooling | ✅ | `scripts/harness_soak.py` + `/soak [since] [session]` Telegram command. Buckets `activity.jsonl` rows by `harness=` field, reports turn counts, outcome rates, p50/p95 duration, OOM/quota/stall counts. 18 tests. |
| 3 — Live soak | running | cc-sdk active in synodic-kit topic; `/soak` for live readout. |
| 4 — Flip default | future | `STARGATE_DEFAULT_HARNESS=cc-sdk`. Keep `cc-cli` as fallback. |
| 5a — Pi harness | next | `badlogicgames/pi` — multi-model coding agent, default deepseek via openrouter. |
| 5b — Aider harness | next | Python coding CLI, model-agnostic. |
| 5c — OpenCode harness | next | sst/opencode, TUI-first coding agent with CLI mode. |
| 5z — codex/cursor/gemini | future | Lower priority. |

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

## What 3 added (on top of 1c)

- `_cancel_session_async(state)` — bridge.py helper that hides the
  cc-cli/cc-sdk split. cc-cli SIGKILLs the proc; cc-sdk schedules
  `harness.cancel()` on the worker thread's event loop via
  `asyncio.run_coroutine_threadsafe` and awaits with a 5s timeout.
- `SessionState.harness` and `SessionState.worker_loop` — populated by
  `_drive_harness_sync` for the lifetime of a turn, cleared in
  `finally`. Used by the cancel helper to know what to do.
- `_iter_active_sessions()` — backend-agnostic replacement for
  `_iter_active_procs()` at every call site that doesn't actually need
  a `Popen` handle (stall detector, /restart, shutdown, /ping). The
  old iterator stays for cc-cli-only paths.
- `ClaudeSdkHarness` gained an `on_progress: Callable[[], None]`
  parameter. The harness invokes it on every SDK message so the
  bridge's `state.last_event_at` advances at the same per-event
  cadence the cc-cli harness already provides via per-stdout-line
  refresh. The stall detector doesn't care which harness fed the
  signal.
- `run_claude` instantiates `ClaudeSdkHarness` when the resolved
  harness is `cc-sdk`. `state.proc` stays None for the duration of
  the turn (the SDK owns its own subprocess); `state.harness` and
  `state.worker_loop` are the only handles `/kill` needs.
- Graceful shutdown stays sync. cc-cli sessions get the existing
  SIGTERM/SIGKILL pump; cc-sdk sessions are logged for the operator
  and rely on parent-exit cleanup (the SDK's child subprocess
  inherits SIGHUP / EOF on stdin when we exit and tears itself down).

## What 1c added (on top of 1b core)

- `STARGATE_DEFAULT_HARNESS` env (defaults to `cc-cli`); validated at
  startup against `VALID_HARNESSES = ("cc-cli", "cc-sdk")`.
- `stargate.projects.get_chat_harness` / `set_chat_harness` —
  per-chat override stored as the optional `"harness"` key on the
  dict-form `chat_projects.json` entry. Setter promotes legacy string
  entries to dict-form so `path` and `agent` siblings survive.
- `/harness [name]` Telegram command — display + set + clear, with
  validation against `VALID_HARNESSES`. Selecting `cc-sdk` warns the
  user that the dispatcher still runs cc-cli until phase 3.
- `bridge.run_claude` consults the per-chat selection (or
  `DEFAULT_HARNESS`). cc-sdk is logged under
  `harness_requested=cc-sdk` but executed under `harness=cc-cli`. This
  is intentional — phase 3 needs proper /kill integration before
  cc-sdk is safe to dispatch.
- Every activity entry that touches a turn (`claude_invoke`,
  `claude_complete`, `claude_error`, `claude_timeout`, `quota_hit`)
  carries `harness=<effective>`. `claude_invoke` additionally carries
  `harness_requested=<requested>` for behavioural diffing during the
  phase-3 soak.

## What's left

Phases 0 / 1a / 1b / 1c / 2 / 3a / 3b have all landed. The remaining
work is the live soak (the original "phase 3" goal):

- Pick one or two low-stakes topics. `/harness cc-sdk`. Watch.
- After a few weeks, compare `harness=cc-cli` vs `harness=cc-sdk`
  rows in `activity.jsonl` for: turn duration distributions, OOM
  rate, rate-limit handoffs, empty-success rate, stall kills,
  surprises.
- If cc-sdk is boring (no regressions, parity on observed metrics)
  for the soak window, flip `STARGATE_DEFAULT_HARNESS=cc-sdk`. Keep
  cc-cli as a per-topic fallback (phase 4 of the table above).
- Phase 5 (codex / cursor / pi) would re-exercise
  `HarnessCapabilities.supports_resume=False`. Out of scope for now.

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
