# Patchbay Relay

Read `CLAUDE.md` for repository architecture and operational workflows. These instructions apply to Codex working in this repository.

1. pi is the default harness; Codex CLI is selectable with `/harness codex`. Keep capabilities and model/session ownership separate between backends.
2. Codex subprocesses use existing ChatGPT subscription authentication, never an inherited API key. `CODEX_MODEL` optionally overrides the CLI-configured model. Backend switches reset the conversation.
3. Run `uv run pytest tests/ -q`, `uv run ruff check .`, and `uv run python validate.py` before shipping changes. Tests must isolate production paths through `tests/conftest.py`.
4. Work on `develop`, explicitly stage changed source files, and preserve live runtime state files. The launchd relay runs from this checkout; deployment requires a safe restart after verification.
