# Harness Design — Stargate Phase 0

Status: **draft for review**. No code changes yet. This doc defines the seam
that lets stargate run multiple coding-agent backends (Claude Code CLI,
Claude Agent SDK, codex/pi, cursor, …) behind a single interface.

The migration plan that motivates this doc is summarized at the bottom.

---

## Goal

Replace the inline `subprocess.Popen([CLAUDE_PATH, "-p", message, ...])` call
in `bridge.run_claude()` with a call through a `Harness` protocol. The
existing CLI path becomes one concrete `Harness`; the Claude Agent SDK
becomes a second; future backends become more.

Non-goals for phase 0: writing any harness, deleting any code, changing any
behavior.

## What stargate actually needs from a "harness"

Distilled from `bridge.run_claude` + `parser.py` + `quota.py` + `self_heal.py`:

1. **Run a turn**: take a user prompt and a working directory, return text
   for Telegram, while emitting structured events along the way (so the
   activity log keeps its fidelity).
2. **Resume**: reuse a session ID from a previous turn so the agent has
   memory across messages. Some harnesses won't support this — interface
   must allow capability negotiation.
3. **Identify the new session ID** the harness wrote to disk (so we can
   `--resume` on the next turn).
4. **Cancel**: a `/kill` command must terminate the active turn cleanly.
5. **Surface failures we already know how to repair**: OOM kill, corrupt
   session storage, rate limit, mid-stream parse error.
6. **Pass our system prompt, allowed/disallowed tools, model, effort,
   plugin dir, MCP servers** through to the underlying agent.

## Protocol stub

```python
# stargate/harness/base.py  (sketch — phase 1 will land the actual code)

from typing import Protocol, AsyncIterator, Literal
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HarnessCapabilities:
    """What the underlying agent supports. Bridge logic branches on this."""
    supports_resume: bool          # can pass a prior session_id and continue
    supports_tool_streaming: bool  # emits per-tool-use events mid-turn
    supports_interrupt: bool       # can cancel a turn cleanly without SIGKILL
    supports_effort: bool          # honors low/medium/high/max effort
    supports_mcp: bool             # can load MCP servers


@dataclass(frozen=True)
class TurnRequest:
    prompt: str
    session_key: str               # stargate's chat:thread key (for logging)
    project_dir: Path
    system_prompt: str
    resume_session_id: str | None  # None => fresh session
    model: str | None              # None => harness default
    effort: str | None             # None => harness default
    allowed_tools: list[str] | None
    disallowed_tools: list[str] | None
    max_turns: int | None          # harness may ignore if unsupported
    plugin_dir: str | None         # claude-specific; harnesses may ignore


# ---- TurnEvent: tagged union streamed back from the harness ----

@dataclass(frozen=True)
class TextDelta:
    text: str                      # incremental assistant text
    final: bool = False            # True on the last delta of a turn

@dataclass(frozen=True)
class ToolUse:
    name: str
    input: dict
    id: str | None = None

@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str | None
    output: str
    is_error: bool = False

@dataclass(frozen=True)
class TurnError:
    """A failure the harness has classified.

    `kind` matches stargate.self_heal repair-handler keys where possible
    (`claude_oom_137`, `corrupt_session_json`, `rate_limit`, …) so the
    bridge can dispatch repairs uniformly.
    """
    kind: Literal[
        "rate_limit",
        "oom",
        "corrupt_session",
        "max_turns",
        "timeout",
        "process_died",
        "unknown",
    ]
    message: str
    retryable: bool                # caller may safely call run_turn again
    metadata: dict | None = None   # harness-specific extras (exit_code, etc.)

@dataclass(frozen=True)
class TurnFinal:
    """Last event of a successful turn. Carries the new session_id so the
    bridge can persist it for next-turn resume."""
    session_id: str | None         # may be None for non-resuming harnesses
    num_turns: int | None
    total_cost_usd: float | None
    raw_text: str                  # full assistant text for Telegram


TurnEvent = TextDelta | ToolUse | ToolResult | TurnError | TurnFinal


class Harness(Protocol):
    """A coding-agent backend. Implementations: ClaudeCliHarness,
    ClaudeSdkHarness, CodexHarness, CursorHarness, …"""

    name: str
    capabilities: HarnessCapabilities

    async def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Yield events for one turn. Caller iterates; harness owns the
        underlying subprocess/connection lifetime. Raises only on
        catastrophic protocol violations — known failures are emitted as
        TurnError events so the caller can decide retry vs. surface."""
        ...

    async def cancel(self) -> None:
        """Best-effort cancel of the current turn. /kill calls this."""
        ...
```

A `TurnEvent` stream always ends with **exactly one** `TurnFinal` *or*
`TurnError`. Any number of `TextDelta`/`ToolUse`/`ToolResult` events may
precede it. This is the contract every harness must honor and every test
must verify.

## How `bridge.run_claude` changes (phase 1, sketch only)

```python
async def run_turn(message: str, session_key: str, ...) -> str:
    harness = select_harness(session_key)             # NEW — chat_projects.json
    req = TurnRequest(prompt=message, ...)             # built like today
    text_chunks: list[str] = []
    async for event in harness.run_turn(req):
        match event:
            case TextDelta(text=t):
                text_chunks.append(t)
            case ToolUse(name=n, input=i):
                _log_activity("tool_use", session_key=session_key, tool=n)
            case TurnError(kind="oom", retryable=True):
                # existing OOM self-heal path
                ...
            case TurnError(kind="rate_limit"):
                return QUOTA_HIT_PREFIX + message
            case TurnFinal(session_id=sid, raw_text=t):
                if sid:
                    save_session_id(session_key, sid)
                return t or "(no parseable response)"
    return "(stream ended without final event)"  # harness contract violation
```

The 300-line `run_claude` collapses to ~30 lines plus the existing
self-heal/quota glue. All knobs that vary per-call (model, effort, system
prompt) move into `TurnRequest`. The system-prompt construction stays in
the bridge — harnesses just pass it through.

## Concrete harnesses (preview — NOT for phase 0)

### ClaudeCliHarness
- Wraps current `Popen` + `_read_proc_streaming` + `_parse_events`.
- `capabilities`: all True except `supports_interrupt` (today we SIGKILL).
- Phase 1 lift-and-shift; behavior identical to today.

### ClaudeSdkHarness
- Uses `claude_agent_sdk.ClaudeSDKClient` with `ClaudeAgentOptions`.
- All current CLI flags map to options:
  | CLI flag | SDK option |
  |---|---|
  | `--resume <id>` | `resume="<id>"` |
  | `--max-turns N` | `max_turns=N` |
  | `--allowed-tools` | `allowed_tools=[...]` |
  | `--disallowed-tools` | `disallowed_tools=[...]` |
  | `--model X` | `model="X"` |
  | `--effort X` | `effort="X"` |
  | `--plugin-dir P` | (likely `extra_args={"plugin-dir": P}` — verify in phase 2) |
  | `--append-system-prompt S` | `system_prompt={"type":"preset","preset":"claude_code","append":S}` |
  | `--dangerously-skip-permissions` | `permission_mode="bypassPermissions"` |
- Streams `AssistantMessage` → `TextDelta`/`ToolUse`, `ResultMessage` → `TurnFinal`.
- `ProcessError(exit_code=137)` → `TurnError(kind="oom")`.
- `CLIJSONDecodeError` → `TurnError(kind="corrupt_session")` (rare; SDK handles framing).
- Rate-limit detection still string-matches `ProcessError.stderr` and any
  `ResultMessage.error` field — the patterns from `quota.py` lift over
  unchanged.

### CodexHarness / CursorHarness (future)
- `supports_resume=False` likely — bridge then concatenates history into
  the prompt itself, or refuses session-mode for these harnesses.
- These are why `HarnessCapabilities` exists.

## Selection

Per-chat harness is stored in `chat_projects.json`:

```json
{
  "-1003884282041_30": {
    "path": "stargate",
    "harness": "cc-cli"
  }
}
```

- Default harness from env: `STARGATE_DEFAULT_HARNESS=cc-cli`.
- New command: `/harness [cc-cli | cc-sdk | codex | …]` flips the topic.
- During phase 1 the only registered harness is `cc-cli` (default). Phase 2
  adds `cc-sdk` opt-in. Phase 4 flips default.

## Open questions — resolved during phase 0

| Q | Answer |
|---|---|
| Does SDK share CLI session storage? | **Yes.** Both write `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`. Cross-resume works. |
| How are quota/rate-limit errors surfaced? | `ProcessError.stderr` and/or `ResultMessage.error` text — same patterns as today, cleaner inputs. |
| Failure isolation? | SDK runs the CLI as subprocess, same as us. **No worse than today.** OOM kills the child, not the bridge. Accept it. |
| Capability gaps for non-Claude harnesses? | `HarnessCapabilities` flags + branch in bridge. `supports_resume=False` is the main one to plan for. |

## Self-heal mapping

Today's `dispatch_repair` keys map cleanly onto `TurnError.kind`:

| `TurnError.kind` | self-heal handler | Notes |
|---|---|---|
| `oom` | `claude_oom_137` | Same retry-with-trimmed-prompt path |
| `corrupt_session` | `corrupt_session_json` | Quarantine + retry fresh session |
| `rate_limit` | (no handler — Forge handoff) | Keeps existing `_maybe_handoff_quota` |
| `process_died` | new — `harness_subprocess_died` | Phase 2 |
| `timeout` | new — `harness_turn_timeout` | Phase 2 |

`stale_telegram_poller` is unrelated to harnesses — stays as-is.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| In-process blast radius from SDK | **Resolved**: SDK still uses a CLI subprocess. Same isolation as today. |
| Self-heal regressions on SDK harness | Phase 3 soak in one test topic; `harness=` field on every `activity.jsonl` line so we can grep behavior diffs. |
| Test re-pointing | Most tests of `_parse_events` / `_is_quota_error` become CLI-harness-only. Harness-contract tests (event ordering, exactly-one-terminal-event) run against both impls via a shared test fixture. |
| Plugin-dir / MCP-server pass-through differences | Confirm in phase 2 with a one-off SDK call before designing around it. If `plugin_dir` isn't a first-class option, `extra_args` is the escape hatch. |

## Migration phases

| Phase | Output | Reversible? |
|---|---|---|
| 0 (this doc) | Design + answers, no code | n/a |
| 1 | Refactor: extract `Harness`, wrap CLI in `ClaudeCliHarness`. Same behavior, all 422 tests pass. | Yes — revert one PR |
| 2 | Add `ClaudeSdkHarness`, opt-in via `/harness cc-sdk` per topic | Yes — leave default on cc-cli |
| 3 | Soak SDK harness in one test topic. Compare behavior. | Yes — `/harness cc-cli` to revert that topic |
| 4 | Flip default to `cc-sdk`. Keep `cc-cli` as fallback. | Hard — but cc-cli still in tree |
| 5 | Add codex/pi, cursor, … as additional harnesses | Per-harness |

## Decision points for review

These are the cheapest moments to disagree before more code is written:

1. **Now**: protocol shape, capability flags, event types. If `TurnEvent`
   doesn't capture something we need, this is the time to add it.
2. **End of phase 1**: real `ClaudeCliHarness` lands. If the seam feels
   wrong in practice, easiest revert.
3. **Before phase 4**: default flip. SDK harness is opt-in until then.

## Appendix: what stays the same

- Session storage on disk (`sessions/{key}.json` for stargate; `~/.claude/projects/...` for the CLI/SDK)
- Pending message replay
- `_process_with_claude_turn` queue/drain logic
- Telegram send/typing/markdown handling
- Quota Forge handoff format
- `activity.jsonl` (gains `harness=` field)
- All commands except a new `/harness`
