---
type: process
title: Split monolithic server.py into focused modules
kind: decision
tags: [code organization, Python, FastAPI]
confidence: 0.9
---
Broke ~470-line `server.py` into 9 files (config, chats, pi_runner, asr, tts, routes, app, etc.). Each file has a single responsibility, aligning with the preference for short, focused files (one top-level function per file, <100 lines).
