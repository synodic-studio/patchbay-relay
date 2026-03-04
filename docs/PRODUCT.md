# Claude Telegram Bridge — Product Overview

## What It Is

Telegram bot that bridges messages to Claude Code sessions. Powers all mobile interaction with Bryan's Fanta agent system. Routes Telegram topic messages to the correct Claude Code instance with per-topic project/agent mapping.

**Audience:** Single-user infrastructure (Bryan).

**Role:** Critical path — if this breaks, mobile access to all agents is down.

## Tech Stack

- **Python 3.13+** with uv
- **FastAPI** — OAuth callback server
- **python-telegram-bot 22.6+** — long-polling bot
- **Sign in with Apple** — authentication (PyJWT)
- **Cloudflare Tunnel** — reverse proxy for OAuth callbacks
- **Uvicorn** — ASGI server
- **launchd** — two persistent services (bridge + auth)

## Current State

**Status: Production.** Actively maintained, both services running continuously.

- Last commit: 2026-03-04
- 7 open beads (auth/security features: TOTP, session timeout, /lock)
- 138 TODOs in codebase (needs triage)
- Recent: quota/rate-limit detection, Pac-Man handoff

## Key Architecture

- `bridge.py` — main Telegram bot, long-polling event loop
- `auth.py` / `auth_server.py` — Sign in with Apple OAuth
- `chat_projects.json` — topic → project/agent routing
- Activity logging to `activity.jsonl`
- Two launchd services: `com.synodic.claude-telegram-bridge` (bot) + `dev.kj6.auth-bridge` (OAuth + tunnel)

## Known Trajectory

Expect eventual deprecation as native Claude Code remote capabilities improve. Until then, this is the only mobile interface to the agent system.

## Connections

- **Fanta** — all agents route through this
- **synodic-kit** — plugin ecosystem (hooks, skills, MCP servers)
