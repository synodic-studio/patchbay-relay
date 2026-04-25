# Harness Design — Stargate

The seam that lets stargate run multiple coding-agent backends (Claude
Code CLI, Claude Agent SDK, codex/pi, cursor, …) behind one interface.

## Status

| Phase | Status | What landed |
|---|---|---|
| 0 — Design | ✅ | This doc + 4 open questions resolved |
| 1a — Protocol + CLI harness | ✅ | `stargate/harness/{base,claude_cli}.py`, 16 tests. Dormant — bridge unchanged. |
| 2 — SDK harness | ✅ | `stargate/harness/claude_sdk.py`, 24 tests. Dormant. Validated the protocol from a 2nd angle without any changes. |
| 1b — Rewire bridge | next | Make `bridge.run_claude` consume the harness stream; delete duplicated Popen/parse logic. Add `chat_projects.json.harness` field + `/harness` command. |
| 3 — Live soak | future | One topic on `cc-sdk` for as long as it takes to be boring. Compare via `harness=` field on `activity.jsonl`. |
| 4 — Flip default | future | `STARGATE_DEFAULT_HARNESS=cc-sdk`. Keep `cc-cli` as fallback. |
| 5 — Other backends | future | codex/pi, cursor — exercises `HarnessCapabilities.supports_resume=False`. |

## Forward plan: phase 1b

The work that remains in this phase:

1. Build `TurnRequest` from per-message + per-chat config inside
   `bridge.run_claude` (system prompt construction stays in the bridge).
2. Iterate `harness.run_turn(req)` and translate the event stream back to
   the existing return-string contract for `_process_with_claude_turn`.
3. Wire `on_progress` (or per-event consumption) to keep
   `SessionState.last_event_at` updating — this is what the stall
   detector reads.
4. Map `TurnError.kind` to existing recovery paths:
   - `oom` / `retryable=True` → existing OOM self-heal retry with trimmed prompt
   - `corrupt_session` → `clear_session()` + one-shot retry
   - `rate_limit` → existing Forge handoff (`_maybe_handoff_quota`)
   - `max_turns` → append the existing turn-limit notice to the text
   - `timeout` → existing "Timed out after N min" message
   - `process_died` (new kind) → surface stderr to user, no retry
5. Delete the now-duplicated Popen/drain/parse logic from `bridge.run_claude`.
6. Add per-chat harness selection: `chat_projects.json` gains an optional
   `"harness": "cc-cli" | "cc-sdk"` field; default from env
   `STARGATE_DEFAULT_HARNESS=cc-cli`. New `/harness <name>` command.
7. Add `harness=` field to every `activity.jsonl` entry that touches a turn.

After 1b ships, phase 3 is just flipping one topic to `cc-sdk` and
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
