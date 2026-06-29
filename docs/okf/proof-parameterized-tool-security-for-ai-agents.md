---
type: proof
title: Built injection-safe pi extension with 12 parameterized tools using argv arrays
capability: Parameterized tool security for AI agents
kind: built
tags: [TypeScript, pi, security, file system tools]
created: 2026-06-28
confidence: 0.95
sources: [29c06c62: feat(voice-demo): custom pi extension with 12 injection-safe tools]
---
Developed a TypeScript extension for pi agent that registers 12 tools (read_file, grep_search, glob_find, etc.) with parameterized arguments passed as arrays, preventing shell injection. The write_file tool is restricted to a specific docs directory at the execution layer, providing defense-in-depth beyond system prompt instructions. All file references are validated against an allowlist regex before execution. This design ensures the agent can operate on the filesystem safely without risk of arbitrary command execution.
