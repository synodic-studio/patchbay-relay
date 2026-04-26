# Stargate Channels MCP — Plan

## Summary

Add an MCP channel server to Stargate for live in-flight message delivery. Purely additive — no changes to existing spawn/resume/response flow.

## Current Behavior

1. Telegram message arrives
2. Stargate checks if a session is already running for that thread
3. If running: queue the message, deliver after current session completes
4. If not: spawn `claude -p --resume <id>` with the message, parse stdout, send reply

## Proposed Change

Add one new path for step 3:

3. If running: push the message into the active session via MCP channel notification. Claude sees it immediately in-flight and can respond via `reply` tool.

Everything else stays the same:
- Spawn logic unchanged
- `--resume` unchanged
- Response parsing unchanged
- `/clearnew`, `/model`, `/kill` and all commands unchanged
- Offline/no-session path unchanged (still spawns `claude -p`)

## Architecture

```
Telegram Bot API
       ↓
Stargate (central dispatcher)
       ↓
       ├── No active session → spawn claude -p (today's path, unchanged)
       │
       └── Active session exists → push via MCP channel notification
           Claude responds via reply/react/edit MCP tools
```

## MCP Channel Server

A lightweight Python MCP server that Stargate controls:

- Declares `claude/channel` capability
- Exposes tools: `reply(chat_id, text)`, `react(chat_id, message_id, emoji)`, `edit_message(chat_id, message_id, text)`
- Stargate pushes messages in via `notifications/claude/channel`
- Tools call back to Stargate's Telegram bot to send responses

The MCP server is spawned as part of each `claude -p` invocation:
```bash
claude -p --channels server:stargate-channel --resume <session_id>
```

## Implementation Steps

1. **Build MCP channel server** (`stargate/channel_server.py`)
   - Declare `claude/channel` capability
   - Implement reply/react/edit tools that POST back to Stargate's internal API
   - Accept notifications from Stargate via stdin/IPC

2. **Add internal API to Stargate** for the MCP server to call back
   - `POST /internal/reply` → sends Telegram message
   - `POST /internal/react` → adds emoji reaction
   - Bound to localhost only

3. **Update `run_claude()` in bridge.py**
   - Add `--channels server:stargate-channel` to the spawn command
   - Register the MCP server in a session-scoped `.mcp.json`

4. **Update message routing in bridge.py**
   - When a message arrives for a thread with an active subprocess:
     - Instead of queuing, push via the MCP server's notification endpoint
   - Track which sessions have the channel MCP active

5. **Test**
   - Single message (no active session) → same as today
   - Second message during active session → delivered in-flight
   - Session ends → next message spawns fresh
   - `/clearnew` → works as before
   - `--resume` → channel reconnects to existing context

## What This Unlocks

- No more queued messages — the user can send follow-ups while Claude is working
- Claude responds to follow-ups naturally in the same context
- No more "your message was queued" friction
- Foundation for richer bidirectional communication (photos, files in-flight)

## Not In Scope

- Replacing the spawn/parse model (keep it)
- Persistent long-lived sessions (stay headless)
- Multiple concurrent sessions per thread
- Changes to any Stargate commands
