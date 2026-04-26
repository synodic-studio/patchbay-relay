# Stargate Channels — Design

In-flight bidirectional comms between Adrien's Telegram and an active
agent turn. Built on the agent's native streaming-input primitive
("channels" colloquially), not a polled MCP tool.

## What changes for Adrien

**Today:** While claude is processing, follow-up messages get queued.
They batch together and dispatch as a single follow-up after the
current turn finishes. There's a "your message was queued" reply.

**With channels:** Follow-up messages get pushed into the active turn
immediately. Claude picks them up at its next natural break (next
tool call → between tool calls → before its next reasoning step).
No queue, no batch, no friction message. The model treats it like a
new user turn in the same conversation context.

Adrien picked the **silent-absorb** behavior: claude doesn't
acknowledge inflight pushes — they just blend into the ongoing work.
Same as how a terminal user typing a follow-up while the model is
streaming feels.

## SDK primitives we use

The Claude Agent SDK exposes the underlying streaming-input mechanism
on `ClaudeSDKClient`:

| Method | What it does |
|---|---|
| `await client.connect(prompt_or_stream)` | Open the persistent connection |
| `await client.query(prompt, session_id)` | Send a user message at *any* time during the conversation |
| `await client.interrupt()` | Cancel the in-flight turn |
| `await client.set_model(name)` | Switch model mid-conversation |
| `await client.set_permission_mode(mode)` | Switch permission mode mid-conversation |
| `async for msg in client` | Receive assistant + system messages as they arrive |

`query()` is callable while a previous turn is still streaming. New
messages enter the SDK's user-message queue and the model consumes them
at its next decision point. This is exactly the "natural absorb"
behavior Adrien asked for.

CLI equivalent: `claude -p --input-format stream-json --output-format
stream-json`. We don't use the CLI form for this work — cc-cli stays
one-shot. Channels are cc-sdk only initially.

## Agnostic seam

Per the stargate principle: the bridge cannot bake cc-sdk specifics
into channel logic. We expose the capability through the harness
protocol so other backends can opt in later (pi via `--mode rpc`,
cc-cli via `--input-format stream-json`, future codex/cursor).

### New capability flag

```python
@dataclass(frozen=True)
class HarnessCapabilities:
    ...
    supports_inflight_push: bool  # NEW. accept new user messages mid-turn
```

| Harness | `supports_inflight_push` |
|---|---|
| cc-sdk | True (via `client.query()`) |
| cc-cli | False (would need stream-json refactor) |
| pi | False (would need `--mode rpc` refactor) |
| aider | False (one-shot CLI, no streaming-input mode) |
| opencode | False (one-shot CLI, no streaming-input mode) |

### New harness methods (protocol extension)

```python
class Harness(Protocol):
    name: str
    capabilities: HarnessCapabilities
    def run_turn(self, req: TurnRequest) -> AsyncIterator[TurnEvent]: ...
    async def cancel(self) -> None: ...

    # NEW — only required when supports_inflight_push=True
    async def open_channel(self, req: TurnRequest) -> "ChannelHandle": ...
```

```python
class ChannelHandle(Protocol):
    """A long-lived agent conversation. Multiple user messages, one client."""
    session_id: str | None  # surfaces the SDK's session_id once it's known
    async def push(self, prompt: str) -> None: ...
    def events(self) -> AsyncIterator[TurnEvent]: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...
```

The default harness method `open_channel` raises
`NotImplementedError`. Callers should check
`capabilities.supports_inflight_push` before invoking it.

### Bridge integration

`SessionState` gains:
- `channel: ChannelHandle | None` — open SDK client, if one is held
- `channel_idle_timer: asyncio.TimerHandle | None` — auto-close after N min idle

`run_claude` flow changes:
- If a channel is currently open for this session_key:
  - If a turn is in-flight, push the new message via `channel.push()` and
    return — no new spawn, no queue.
  - If idle but channel still open, push (next turn starts immediately
    with shared context).
- If no channel and `harness.capabilities.supports_inflight_push`, open
  one (via `harness.open_channel`) and push the prompt.
- Otherwise (legacy harnesses): existing one-shot path. No behavior
  change.

Idle timer: 5 min. After that the channel closes; next message reopens.
This keeps long-lived subprocess count bounded. Configurable via
`STARGATE_CHANNEL_IDLE_SECONDS`.

### Cancellation parity

`/kill` continues to work unchanged: if a channel is open with an
in-flight turn, `_cancel_session_async` calls `channel.interrupt()`
on cc-sdk. The channel itself stays open after interrupt (idle timer
restarts). `/kill` again with no in-flight turn closes the channel.

### Activity logging

Existing entries (`claude_invoke`, `claude_complete`, etc.) keep their
shape. We add:
- `channel_open` — when a long-lived client opens
- `channel_push` — when a new message lands on an existing channel
  (instead of `claude_invoke`)
- `channel_close` — when a channel closes (idle timeout, /clearnew,
  shutdown)

These plus the existing `harness=` field let `/soak` distinguish
channel-mode turns from one-shot turns when comparing harnesses.

## What's *not* in scope

- **Outbound spontaneous updates** ("Found 3 issues, fixing now"). The
  SDK doesn't natively let claude push proactive Telegram messages
  during a tool call. That would still need a `notify` MCP tool. Out of
  scope for the channels work — separate feature, separate skill.
- **Long-running tool calls.** If claude spends 5 minutes inside a
  single bash call, the inflight message can't be delivered until that
  call completes. SDK limitation. Acceptable today.
- **Cross-harness channel migration.** If a chat switches harness
  mid-conversation (`/harness pi`), any open cc-sdk channel closes; the
  next message starts fresh on the new harness. No magic.

## Tuning presets — relationship

Tuning presets (terse / balanced / deep-dive / teach-me / autonomous)
inject system-prompt snippets. Presets and channels are orthogonal:
- Channels = transport (mid-flight push)
- Presets = system-prompt shape

When channels are open and Adrien runs `/preset deep-dive`, the change
takes effect on the *next* user message (the next `channel.push()`).
The system prompt is set per-message via the SDK's `system_prompt`
option, so re-pushing with the new preset is enough.

## Implementation plan

| Step | What | Status |
|---|---|---|
| 1 | Add `supports_inflight_push` to `HarnessCapabilities`, `ChannelHandle` and `ChannelCapableHarness` protocols. cc-sdk advertises True. | ✅ |
| 2 | Implement `ClaudeSdkChannel` (`stargate/harness/claude_sdk_channel.py`). Wraps a long-lived `ClaudeSDKClient`. push/events/interrupt/close. 17 unit tests + smoke-tested with real SDK including a true mid-flight push (mid-turn redirect arrives, model finishes current turn, picks up redirect cleanly). | ✅ |
| 3 | Bridge integration. **Pending — needs design discussion before landing.** See "Bridge integration plan" below. | ⏳ |
| 4 | Activity events (`channel_open` / `channel_push` / `channel_close`). Update `/soak` to recognize them. | Blocked on (3) |
| 5 | Bridge-dispatch tests. | Blocked on (3) |
| 6 | CLAUDE.md / HARNESS-DESIGN.md cross-link. | After (3) |

## Bridge integration plan (step 3, pending)

The today's bridge runs each turn through `_drive_harness_sync`, which creates a *fresh* asyncio loop per call (`asyncio.run(_drive())`). For channels we need a persistent loop holding the long-lived `ClaudeSDKClient` across multiple user messages. Three subtle places need careful work:

1. **Worker-loop lifetime.** Either keep a long-lived loop on the worker thread (loop-per-session_key, retired on idle close) or refactor to a single shared loop. The current "fresh loop per turn" model is incompatible with persistent channels because the SDK can't cross async runtime contexts.

2. **Queue/drain interaction.** `_process_with_claude_turn` claims the lane, runs one turn, drains queued follow-ups, then releases. With channels, follow-ups should be `channel.push()` calls, not new `run_claude` invocations. The "lane" model itself may need to become "channel held" rather than "turn in flight."

3. **/kill / /clearnew / shutdown.** Today /kill SIGKILLs cc-cli or cancels the cc-sdk task. With channels, /kill should `channel.interrupt()` (preserving the channel) and a separate `/close` should `channel.close()`. /clearnew should `channel.close()` + delete session id. Shutdown must close all channels.

**Suggested incremental path:**

- (3a) Gate behind `STARGATE_CHANNELS=1` env (default off) so production is unaffected.
- (3b) Add `SessionState.channel`, `channel_consumer_task`, `channel_idle_timer`. Add a NEW helper `_dispatch_via_channel(session_key, prompt, send_cb)` that opens the channel if absent and pushes. The consumer task reads `channel.events()` and calls `send_cb(text)` on each `TurnFinal`.
- (3c) Branch `handle_message` (or `_process_with_claude_turn`) on (channel-flag enabled AND harness.capabilities.supports_inflight_push). New path bypasses queue. Old path unchanged.
- (3d) Idle timer: 5 min from last final. Reset on each push. Configurable via `STARGATE_CHANNEL_IDLE_SECONDS`.
- (3e) Wire /kill → `channel.interrupt()`; /clearnew → `channel.close()`; shutdown → close all.
- (3f) Tests for each lifecycle transition. Soak in synodic-kit topic for a week. Then flip `STARGATE_CHANNELS=1` as default.

This is one focused PR after the design discussion lands. **Don't merge piecemeal** — channel-mode dispatch + lifecycle + cancel parity must arrive together for the soak to be meaningful.

## Open questions for follow-up discussion

- **Idle timeout** — 5 min default, but should it match the typing-debounce window? Adjacent question: do we close the channel when Adrien stops typing or when the model goes idle? Different semantics.
- **Channel during /clearnew** — close the channel cleanly, or interrupt + close? I'm leaning interrupt + close so /clearnew is reliably "kill all state."
- **Telemetry on inflight pushes** — count per session as a soak metric. Is `/soak` the right surface or do we need a dedicated `/channels` command?
- **Reconnect across bridge restart** — if stargate restarts, all open channels die. The next message reopens via `--resume` (existing behavior). Is that good enough, or do we want to persist channel state? My take: good enough.
