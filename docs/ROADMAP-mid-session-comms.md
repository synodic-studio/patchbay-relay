# Roadmap: Mid-Session Communication via MCP

**Status:** Planning (2026-03-14)
**Problem:** While Claude processes a message (`claude -p`), Adrien is locked out. Messages queue until the session ends. No mid-task dialogue, no status updates, no redirects.

**Goal:** Claude can send non-blocking messages during execution. Adrien can reply at any time. Claude gets notified of replies automatically.

## Architecture

Keep `claude -p` as the launch mechanism. Add two new components:

### 1. Telegram MCP Server (stdio, ~80 lines Python)

Registered in stargate's `.mcp.json`. Available to every Claude session spawned by the bridge.

**Tools:**
- `send(thread_id, text)` — Send message via Bot API. Non-blocking. Returns immediately.
- `ask(thread_id, question)` — Send message via Bot API, mark as "awaiting reply." Non-blocking.

Uses the same `TELEGRAM_BOT_TOKEN` the bridge already uses. No Telethon, no user account, no phone number.

### 2. PostToolUse Hook (~40 lines Python)

Runs after every Claude tool call. Checks `.stargate-inbox/<session_key>.jsonl` for new messages from Adrien. If found, injects them into Claude's context as hook output.

Claude sees: `"New message from Adrien: <text>"` — no explicit polling needed.

### 3. Bridge Modification (~20 lines in bridge.py)

When `_processing_sessions` has an active key and an incoming message arrives:
- Write to `.stargate-inbox/<key>.jsonl` (for the hook to pick up)
- Still debounce-queue as fallback (in case hook doesn't fire before session ends)

## Message Flow

```
Adrien sends message while Claude is working
    │
    ▼
Bridge writes to .stargate-inbox/<session_key>.jsonl
    │
    ▼
PostToolUse hook fires after Claude's next tool call
    │
    ▼
Hook reads inbox file, injects message into Claude's context
    │
    ▼
Claude reacts (responds via MCP send, adjusts work, etc.)
```

```
Claude wants to update Adrien mid-task
    │
    ▼
Claude calls MCP send(thread_id, "Found 3 issues, fixing now")
    │
    ▼
MCP server calls Bot API → message appears in Telegram
    │
    ▼
Claude continues working (non-blocking)
```

## Session Lifecycle Scenarios

**Claude asks, Adrien replies quickly:**
Hook picks up reply within seconds (next tool call). Claude continues with the answer.

**Claude asks, Adrien doesn't reply, session ends:**
Reply arrives later → bridge starts new `claude -p --resume` → Claude has full conversation context and sees the answer. Already works today.

**Claude asks, nothing else to do:**
Session ends naturally. Adrien's reply triggers a new `--resume` session. The 2-3s startup cost is negligible vs. the minutes Adrien takes to reply.

## Open Design Questions

- **Hook injection volume:** Inject full message text, or just notify "N new messages — call check_inbox"? Full text is simpler but could bloat context on chatty sessions.
- **`ask` vs `send` distinction:** Should `ask` mark the message so the bridge knows to re-invoke Claude on reply? Or just treat all replies equally?
- **Per-session vs daemon MCP:** stdio per-session is simpler (no state management). Daemon allows cross-session state but adds complexity.
- **Long-running Bash commands:** Hooks don't fire during a single long tool call (e.g., 10-min build). Acceptable tradeoff? Or add a timeout-based check?

## What NOT to Use

- **chigwell/telegram-mcp (73 tools):** Requires Telethon + real phone number (user API). No incoming message listener. Overkill — we need 2 tools, not 73. Account ban risk from automation.
- **Telethon / MTProto:** Wrong API for this. Bot API is simpler, safer, and we already have a token.
- **Webhook/SSE MCP transport:** stdio is correct for per-session lifecycle.

## Dependencies

- None new. Bot API via `python-telegram-bot` (already installed). MCP via `fastmcp` or raw stdio.

## Risk

Low. All new components are additive:
- MCP server is opt-in (only active if registered in `.mcp.json`)
- Hook is opt-in (only active if wired in settings)
- Bridge fallback (debounce queue) still works if hook/MCP aren't present
- No changes to auth, session management, or core routing

## Prior Art

- [qpd-v/mcp-communicator-telegram](https://github.com/qpd-v/mcp-communicator-telegram) — 43 stars. Bot API MCP with `ask_user` (blocks for reply) and `notify_user`. Closest to what we want but uses blocking pattern (we want non-blocking + hook notification).
- [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) — 2K stars. Full bridge with project threads but no mid-session comms and no agent identity system.
