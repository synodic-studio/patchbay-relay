---
type: proof
title: Implemented delivered-flag pattern to prevent message loss on SIGTERM
capability: Reliable message delivery across crashes
kind: designed
tags: [Python, asyncio, file I/O, crash recovery]
created: 2026-04-27
confidence: 0.95
sources: [d20faf08: Pending hardening: messages survive SIGTERM mid-send and mid-debounce]
---
Redesigned the pending message system to survive process termination. The key insight is that `asyncio.CancelledError` (raised on SIGTERM) is a BaseException not caught by regular except clauses, so the pending file would be left uncleared. By setting a `delivered = True` flag only after successful `_send_response` returns, and only clearing the pending file when delivered is True, the system guarantees that any message that hasn't been delivered when the process dies will remain in the pending file for replay on restart. Also hardened queued message handling to survive tear-down.
