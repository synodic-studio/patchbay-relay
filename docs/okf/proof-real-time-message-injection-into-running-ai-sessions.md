---
type: proof
title: Wired bridge to route mid-turn messages into active SDK sessions instead of queueing
capability: Real-time message injection into running AI sessions
kind: built
tags: [Python, Claude SDK, asyncio, channel architecture]
created: 2026-05-12
confidence: 0.9
sources: [d8c3f9a8: bridge: route mid-turn cc-sdk messages into the live SDK session instead of queueing]
---
Implemented mid-turn message injection for the cc-sdk harness. When a second Telegram message arrives while a turn is running, instead of queuing it and starting a fresh subprocess after completion, the bridge now sends it directly into the live SDK session via `_live_client` and `_inflight_queries` tracking. This removes the 30-60s overhead of spawning a new process and makes the interaction feel conversational. Also contributed to the decision to retire the cc-cli harness after validation showed ~35% lower latency on the SDK path.
