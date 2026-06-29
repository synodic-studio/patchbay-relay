---
type: proof
title: Built MOP enforce mode with retry loop and edit rewrite for AI agent output regulation
capability: MOP (Model Output Protocol) integration
kind: built
tags: [Python, MOP, Claude SDK, asyncio, pydantic-ai, Haiku]
created: 2026-05-04
confidence: 0.95
sources: [05e4b094: mop: enforce mode with reject retry loop, edit rewrite, stop hook, 794e56e1: patchbay(cc-sdk-mop): add build_options() — v2 in-process MCP + Stop hook + protocol_prompt, efde37a1: harness: add cc-sdk-mop with MOP output filtering (audit mode), d242579c: mop: switch to claude -p backend + 7 harness tests, fea941f4: fix(cc-sdk-mop): set options.resume so sessions persist across turns]
---
Designed and implemented a multi-mode output filtering system for AI coding agents. In enforce mode, the system evaluates each turn against active MOP rules, and can reject (retry with guidance), edit (rewrite via Haiku), or pass through. Built in-process MCP server, Haiku evaluator using pydantic-ai, Telegram deliver closure, and Stop hook callback. Switched from SDK to claude -p backend to use Max plan instead of API billing. Added audit logging with JSONL auditor. This ensures agent outputs meet safety/content policies without degrading workflow.
