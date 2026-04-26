# Channels Fallback for Non-CC-SDK Harnesses

How non-cc-sdk harnesses (cc-cli, pi, aider, opencode) can deliver the
same "user pushes a follow-up while a turn is in flight" experience
that cc-sdk gets natively via the streaming-input primitive.

## Why we need a fallback

cc-sdk has `client.query()` which can be called mid-conversation. That
works because the SDK keeps the connection open across turns and the
model picks up new user messages at its next decision point.

The other harnesses are one-shot subprocesses: each turn is a fresh
process. There's no "live connection" to push into. Stargate's existing
behavior — queue follow-ups, batch them after the current turn ends —
already exists for these cases. The fallback is about getting closer
to the cc-sdk feel without rewriting any of the underlying agents.

The agnostic principle in CLAUDE.md says new features must work across
all harnesses where the underlying capability allows. Channels are a
case where the capability genuinely differs. The fallback is the
"degrade gracefully" half of the principle.

## Three approaches considered

### A — Inbox file + PostToolUse hook

- Stargate writes incoming messages to a per-chat file: `.agents/inbox/<session_key>.jsonl`.
- A PostToolUse hook (claude / pi / opencode all support hooks) fires after each tool call. The hook reads any new lines from the inbox file and injects them into the agent's context via stdout.
- The agent sees: "New message from Bryan: <text>" — at its next turn boundary, indistinguishable from a normal user message.
- Inbox lines are consumed (truncated) as soon as the hook surfaces them.

**Pros:** universal pattern. Works on any agent that supports PostToolUse hooks. No per-harness Python work.

**Cons:** doesn't fire during a single long-running tool call (the famous 5-minute bash command). Acceptable today since most tool calls are short.

### B — Pi extension

Pi supports first-party extensions (`pi --extension <path>`). An extension can register hooks, slash commands, and tools.

- Build a tiny stargate-inbox extension that polls a file every N tool calls and surfaces new messages.
- Distribute the extension as part of stargate (check it in under `stargate/extensions/pi-inbox/`).
- Wire pi harness to load the extension automatically when the chat is in channel mode.

**Pros:** clean integration with pi's native lifecycle. Could grow to more than just inbox (e.g. expose stargate's own tools to pi).

**Cons:** pi-specific. Doesn't solve the problem for aider or opencode. Effort doesn't generalize.

### C — Stargate-owned MCP server

Stargate runs a small MCP server that exposes:
- `check_inbox()` — returns any new messages since the last call.
- `notify(text)` — sends a message back to telegram (the outbound channel idea, also pinned).

Attach the MCP server to non-cc-sdk harnesses that support MCP (cc-cli does, pi probably does, aider/opencode less clear).

**Pros:** standard protocol, multi-harness reuse, gives us outbound proactive messaging too.

**Cons:** more moving parts (MCP server lifecycle, auth, port binding). Some harnesses don't support MCP at all (aider). The "agent has to call check_inbox() to see messages" requires either prompting the agent to poll or adding a hook anyway — defeats the purpose unless paired with (A).

## Recommended path

Start with (A) — inbox file + PostToolUse hook.

- Smallest blast radius. One file path, one hook script.
- Works on cc-cli and pi today (both support PostToolUse). Extends to opencode once we confirm hook support there.
- Aider has no hook system → fallback to existing queue+batch behavior. That's fine; aider users get the cc-sdk-but-degraded experience that already exists.
- Doesn't preclude (C) later if we want outbound proactive messaging from non-cc-sdk agents.

## Per-harness fit

| Harness | Native channels | Fallback approach |
|---|---|---|
| cc-sdk | Yes (`client.query()`) | N/A |
| cc-cli | No | Inbox file + PostToolUse hook |
| pi | No | Inbox file + PostToolUse hook |
| opencode | No | Inbox file + PostToolUse hook (if hooks supported) |
| aider | No | Queue+batch (existing behavior, no fallback work) |

## Implementation sketch

Three pieces, each small.

### Piece 1 — Inbox file convention

- Path: `<project_dir>/.agents/inbox/<session_key>.jsonl`.
- Format: one JSON object per line: `{"ts": <unix>, "text": "<message>"}`.
- Stargate's bridge writes a line on each Telegram message that arrives during an active turn for that session.
- The `.agents/inbox/` directory is gitignored.

### Piece 2 — Hook script

- A single Python script: `stargate/hooks/inbox_check.py`.
- Reads `STARGATE_SESSION_KEY` and `STARGATE_PROJECT_DIR` from env (set by the harness when it spawns the agent).
- Truncate-and-emit: read all lines, atomically truncate the file, emit the messages on stdout in the format the host agent expects (claude: JSON hook output; pi: TBD).

### Piece 3 — Per-harness wiring

- cc-cli: bridge passes `--hook PostToolUse:<script>` when channel mode is on for the chat.
- pi: equivalent flag (need to confirm pi's hook flag format).
- opencode: equivalent flag (need to confirm opencode supports it).

## What this does NOT solve

- **Long-running tool calls.** Hook only fires between tool calls. A single 5-minute bash command holds delivery for 5 minutes. cc-sdk has the same limitation in practice (the model has to reach a decision point).
- **Outbound proactive messages from the agent.** That's a separate feature (the `notify` MCP tool idea). Out of scope for the channels fallback specifically.
- **Aider.** No hook system exists. Aider channel-mode falls back to the current queue+batch behavior. Acceptable.

## Decision points needed before building

- Does pi's PostToolUse hook accept a Python script directly, or does it need a wrapper?
- Does opencode have a hook system at all? (Check `opencode --help` for hook-related flags.)
- Should the inbox file live in `<project_dir>/.agents/inbox/` (project-local, follows the chat) or in stargate's own data dir (separates user data from project)? Lean project-local — agent already has access; less env-passing.
- How does this interact with the existing queue+batch fallback? Probably: channel-mode on → inbox path; channel-mode off → queue+batch. Per-chat opt-in via `/channels on`.
