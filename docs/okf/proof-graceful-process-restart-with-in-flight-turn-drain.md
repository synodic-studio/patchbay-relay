---
type: proof
title: Designed restart protocol that drains active AI sessions before exit
capability: Graceful process restart with in-flight turn drain
kind: designed
tags: [Python, asyncio, process management, reliability]
created: 2026-05-03
confidence: 0.95
sources: [95e355cc: feat(restart): drain mode by default — preserve in-flight turns]
---
Designed and implemented a drain-mode restart for the bridge. On `/restart`, new messages are blocked and the system waits for all in-flight AI turns to finish (up to `RESTART_DRAIN_TIMEOUT`, default 10 min). Only after the drain timeout or natural completion does the system terminate remaining processes and exit. A `force` flag skips the drain for emergency restarts. This preserves in-progress work and avoids burning tokens on responses that will never be delivered.
