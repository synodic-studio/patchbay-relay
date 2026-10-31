# Codex Harness Implementation Plan

**Goal:** Make subscription-authenticated Codex selectable beside pi without changing existing topics.

**Architecture:** Add CodexHarness behind the existing TurnEvent protocol. Keep pi as the default, isolate model choices per harness, and tag persisted sessions with their owning harness.

**Tech Stack:** Python, Codex CLI JSONL, pytest, Ruff.

1. Add failing adapter tests for fresh/resumed turns, tools, failures, cancellation, and timeout. Implement CodexHarness using the installed CLI flags and existing ChatGPT login, passing prompts over stdin and rejecting incomplete or failed runs.
2. Add failing routing/session tests. Register codex capabilities, dispatch turns and inquiries by topic selection, and prevent cross-harness session resume.
3. Add failing command/model tests. Preserve pi aliases and model settings, add Codex model IDs and defaults, and clear the conversation when changing harness.
4. Update repository guidance and environment examples. Verify the complete test suite, Ruff, pre-flight validation, then run a real Codex fresh/resume smoke test.
5. Review the diff, commit explicitly selected files on develop, push, and reload the running relay through its existing drain mechanism when present.

Review focus: subscription auth cannot silently fall back to API billing; pi models cannot leak into Codex; sessions cannot cross harnesses; failed processes cannot report success; unsupported usage/context queries must describe Codex accurately.
