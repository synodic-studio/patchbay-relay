# Codex Model Picker Implementation Plan

**Goal:** Offer subscription-available Codex models as Telegram buttons and reject unavailable manual choices.

**Architecture:** Query the installed Codex app-server under the same ChatGPT authentication restriction as turns. Use account/read and paginated model/list without starting inference. Bind model callbacks to their harness and normalize duplicate API error envelopes into readable replies.

1. Add failing catalog tests for subscription auth, model identity/display labels, hidden models, pagination, timeout, unavailable CLI and cleanup. Implement `list_codex_models() -> list[tuple[str, str]]` and `CodexModelsUnavailable` in `patchbay/harness/codex_models.py`.
2. Add failing selection tests in `tests/test_models_harness.py` and `tests/test_codex_model_validation.py`. Populate `/model` buttons from that catalog, move blocking discovery off the Telegram event loop, validate typed choices, and reject stale menus without changing stored preferences.
3. Add adapter regressions for the repeated nested model_not_found JSON reported by User. Preserve recoverable diagnostics, deduplicate failures and return a clear `/model` instruction.
4. Verify live catalog/authentication without printing account details, remove existing unavailable topic overrides only after checking the default is available, and update current-state docs.
5. Run full warning-free tests, Ruff lint and formatting, pre-flight validation and independent review. Commit and push on develop; restart the relay safely and verify startup.

Review focus: no API-key fallback, no guessed catalog, no cross-harness writes from stale menus, no live production state writes in tests, and no raw duplicate model errors in replies.
