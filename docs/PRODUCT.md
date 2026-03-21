# Claude Telegram Bridge — Product Overview

## What It Is

Telegram bot that bridges messages to Claude Code sessions. Powers all mobile interaction with agent systems. Routes Telegram topic messages to the correct Claude Code instance with per-topic project/agent mapping.

**Audience:** Single-user infrastructure.

**Role:** Critical path — if this breaks, mobile access to all agents is down.

## Tech Stack

- **Python 3.13+** with uv
- **FastAPI** — OAuth callback server
- **python-telegram-bot 22.6+** — long-polling bot
- **Sign in with Apple + TOTP** — authentication (PyJWT, pyotp)
- **Cloudflare Tunnel** — reverse proxy for OAuth callbacks
- **Uvicorn** — ASGI server
- **launchd** — two persistent services (bridge + auth)

## Current State

**Status: Production.** Actively maintained, both services running continuously.

## Key Architecture

- `bridge.py` — main Telegram bot entrypoint, long-polling event loop
- `stargate/` — core package (config, sessions, parser, quota, activity, projects)
- `auth.py` / `auth_server.py` — Sign in with Apple OAuth + TOTP
- `validate.py` — pre-flight validation, run before every bridge start and in CI
- `chat_projects.json` — topic → project/agent routing
- Activity logging to `activity.jsonl`
- Two launchd services: `com.synodic.stargate` (bot) + `dev.kj6.auth-bridge` (OAuth + tunnel)
- Self-healing: crash-loop detection triggers Claude Code auto-fix sessions

## Known Trajectory

Expect eventual deprecation as native Claude Code remote capabilities improve. Until then, this is the only mobile interface to the agent system.

## Connections

- **Fanta** — all agents route through this
- **synodic-kit** — plugin ecosystem (hooks, skills, MCP servers)
