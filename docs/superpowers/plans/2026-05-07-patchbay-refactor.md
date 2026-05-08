# Patchbay Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land four pending refactors against the live bridge: commit the MOP amnesia fix, de-hardcode the `model-output-protocol` path in `pyproject.toml`, move two stray test files into `tests/`, and split `bridge.py` (3,295 lines) into `patchbay/commands/` so command handlers live next to their dependencies.

**Architecture:** Three independent micro-tasks (Tasks 1–3) run in parallel via subagents — they touch disjoint files and can land in any order. Then a sequential split (Tasks 4–7) carves command handlers out of `bridge.py` into a new `patchbay/commands/` package. Approach for the split: introduce a `patchbay/runtime.py` module that exposes the bridge's process-wide state (running processes, send helpers, lifecycle flags) so command modules can import what they need without circular imports back into `bridge.py`. Each command group is moved as one task with TDD-style verification (full pytest run + smoke import) before commit.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `python-telegram-bot 22.x`, `claude-agent-sdk`. Existing test suite is 770 tests at green; the contract for every refactor task is "all 770 pass + ruff clean + bridge imports under the bridge.py smoke test."

---

## Pre-flight context

- Working tree currently has uncommitted MOP work: `bridge.py:691` resume fix, `patchbay/harness/claude_sdk_mop.py` Stop-hook diagnostic logging, `patchbay/mop_deliver.py` deliver logging, `patchbay/mop_verbose.py` (new file), `.env.example` update, `tests/test_claude_sdk_mop.py` regression test, `.beads/issues.jsonl`. These must commit first (Task 0) — the live bridge (PID 22926) is already running these fixes loaded from disk via the launchd respawn, so the code-on-disk and the running process are in sync; a commit just promotes them out of "dirty working tree."
- The bridge's command handlers (`cmd_start` through `cmd_ping`) live in `bridge.py:1765–2954`. Eighteen handlers, registered in `bridge.py:3255–3275`.
- Stray test files at the repo root: `test_bridge.py`, `test_cmd_setproject.py`. They are NOT discovered by `pytest` because `pyproject.toml` has `testpaths = ["tests"]`. Today they're dead code that nobody runs.
- `pyproject.toml:26` pins `model-output-protocol = { path = "/Users/bryancostanza/Developer/model-output-protocol", editable = true }`. The repo lives at `~/Developer/patchbay-relay`; the dependency lives at `~/Developer/model-output-protocol`. A relative path `../model-output-protocol` works from any clone in `~/Developer/`.

## File structure (post-refactor)

**New files:**
- `patchbay/runtime.py` — shared bridge state and helpers that command handlers need (running process registry, `_send_response` wrapper, lifecycle flags, get_session_id helper). Module-level singletons live here instead of in `bridge.py`.
- `patchbay/commands/__init__.py` — exports `register_handlers(app)` that wires all `CommandHandler`s onto the telegram Application.
- `patchbay/commands/lifecycle.py` — `cmd_start`, `cmd_clearnew`, `cmd_kill`, `cmd_cancel`, `cmd_restart`, `cmd_ping`.
- `patchbay/commands/project.py` — `cmd_setproject`, `cmd_project`, `cmd_model`, `cmd_effort`, `cmd_harness`, `cmd_remote_control`.
- `patchbay/commands/observability.py` — `cmd_health`, `cmd_activity`, `cmd_soak`, `cmd_usage`.
- `patchbay/commands/context.py` — `cmd_context`, `cmd_compact`.

**Modified:**
- `bridge.py` — drops 18 cmd handlers (≈1,200 lines removed), drops their helpers if they're only used by one cmd; calls `patchbay.commands.register_handlers(app)` from `main()`. Final size target: under 2,200 lines.
- `pyproject.toml` — relative path to model-output-protocol.
- `tests/` — gains `test_bridge_startup.py` and `test_cmd_setproject_keyboard.py`.

**Deleted:**
- `test_bridge.py`, `test_cmd_setproject.py` (root level).

Each command module has one clear responsibility: lifecycle commands change session state, project commands bind/inspect topic-to-project state, observability commands read activity logs and bridge state, context commands manipulate the running session's window.

---

## Task 0: Commit current MOP fixes (pre-work)

**Files:**
- Modify: working-tree commit only — no code changes

- [ ] **Step 1: Verify tests still green**

Run: `uv run pytest tests/ -q`
Expected: `770 passed`

- [ ] **Step 2: Stage MOP-related changes**

```bash
git add bridge.py \
        patchbay/harness/claude_sdk_mop.py \
        patchbay/mop_deliver.py \
        patchbay/mop_verbose.py \
        tests/test_claude_sdk_mop.py \
        .env.example
```

- [ ] **Step 3: Commit**

```bash
git commit -m "fix(cc-sdk-mop): set options.resume so sessions persist across turns

The cc-sdk-mop dispatch in bridge.py built ClaudeAgentOptions but never
assigned options.resume = session_id, so every turn started a fresh
SDK session despite activity log claiming resume=True.

Also adds:
- MOP_VERBOSE mode (patchbay/mop_verbose.py) — surfaces every verdict
  to chat (accepted line is now message-text-free per Adrien's ask).
- Diagnostic logging in stop_hook_callback and mop_deliver.deliver to
  catch the next Stop-block-without-submit failure if it recurs.
- Regression test test_run_claude_cc_sdk_mop_v2_sets_resume_when_session_exists
  that fails on master (mocked get_session_id to None hid the bug).
- MOP_RULES_DIR documented in .env.example pointing at
  model-output-protocol/rules/active."
```

- [ ] **Step 4: Stage and commit beads sync separately**

```bash
git add .beads/issues.jsonl
git commit -m "beads: sync session 30 amnesia incident notes"
```

- [ ] **Step 5: Verify clean working tree**

Run: `git status --short`
Expected: empty output

---

## Task 1: De-hardcode `model-output-protocol` path in pyproject.toml

**Files:**
- Modify: `pyproject.toml:26`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pyproject_no_hardcoded_paths.py`:

```python
"""Guard: pyproject.toml must not contain absolute paths from a developer's home dir."""

import re
from pathlib import Path


def test_pyproject_has_no_absolute_user_paths():
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    # Match /Users/<name>/ or /home/<name>/ — anything user-specific
    matches = re.findall(r"(/Users/[^/]+/|/home/[^/]+/)", pyproject)
    assert not matches, (
        f"pyproject.toml contains hardcoded user paths: {matches}. "
        "Use relative paths (e.g. ../foo) or env-driven sources instead."
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pyproject_no_hardcoded_paths.py -v`
Expected: FAIL — finds `/Users/bryancostanza/` at line 26.

- [ ] **Step 3: Make path relative**

Edit `pyproject.toml:26`:

```toml
[tool.uv.sources]
model-output-protocol = { path = "../model-output-protocol", editable = true }
```

- [ ] **Step 4: Verify uv resolves the new path**

Run: `uv sync --reinstall-package model-output-protocol 2>&1 | tail -5`
Expected: success, no path errors.

- [ ] **Step 5: Run the new test and full suite**

Run: `uv run pytest tests/ -q`
Expected: `771 passed` (770 + new guard test).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/test_pyproject_no_hardcoded_paths.py
git commit -m "build: use relative path for model-output-protocol dep

The previous absolute path /Users/bryancostanza/Developer/model-output-protocol
broke any clone outside that user's home. Adds a guard test that fails on
any /Users/<name>/ or /home/<name>/ path in pyproject.toml."
```

---

## Task 2: Move stray test files into `tests/`

**Files:**
- Delete: `test_bridge.py` (root)
- Delete: `test_cmd_setproject.py` (root)
- Create: `tests/test_bridge_startup.py`
- Create: `tests/test_cmd_setproject_keyboard.py`

- [ ] **Step 1: Inspect each file to confirm they're independent**

Run: `head -20 test_bridge.py test_cmd_setproject.py`
Expected: `test_bridge.py` is `TestAllowedUserIdsParsing` (subprocess startup tests); `test_cmd_setproject.py` is keyboard rendering tests with a `_stub_modules()` shim. Neither overlaps with files already in `tests/`.

- [ ] **Step 2: Move test_bridge.py**

```bash
git mv test_bridge.py tests/test_bridge_startup.py
```

The `BRIDGE_DIR = Path(__file__).parent` shim now resolves to `tests/`, but the subprocess runs `python -c "import bridge"` with `cwd=BRIDGE_DIR`. Update so cwd is the repo root.

Edit `tests/test_bridge_startup.py:8`:

```python
BRIDGE_DIR = Path(__file__).parent.parent
```

- [ ] **Step 3: Move test_cmd_setproject.py**

```bash
git mv test_cmd_setproject.py tests/test_cmd_setproject_keyboard.py
```

No path adjustments needed — the file uses `sys.modules` shims, not filesystem paths.

- [ ] **Step 4: Run the suite to confirm both files run and pass**

Run: `uv run pytest tests/test_bridge_startup.py tests/test_cmd_setproject_keyboard.py -v`
Expected: all tests in both files pass.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: `≥ 781 passed` (770 + 9 in test_bridge_startup + 4 in test_cmd_setproject_keyboard).

- [ ] **Step 6: Commit**

```bash
git add tests/test_bridge_startup.py tests/test_cmd_setproject_keyboard.py
git commit -m "test: move stray root test files into tests/

test_bridge.py and test_cmd_setproject.py at the repo root were never
discovered by pytest because pyproject.toml sets testpaths = ['tests'].
Moving them inside tests/ activates them — adds ~13 tests to the suite."
```

---

## Task 3: Extract shared runtime helpers into `patchbay/runtime.py`

This is the prerequisite for moving command handlers. Identifies what bridge module-globals the cmd handlers consume, then relocates them to a module both `bridge.py` and `patchbay/commands/*` can import without cycles.

**Files:**
- Create: `patchbay/runtime.py`
- Modify: `bridge.py` — replace internal references with `from patchbay.runtime import ...`

- [ ] **Step 1: Inventory what cmd handlers reference**

Run: `grep -nE "^async def cmd_" bridge.py`
For each handler, scan its body for unqualified names that resolve to module-level state (process registries, `_send_response`, `get_session_id`, `chat_projects` dict, `_BRIDGE_STARTED_AT`, etc.).

Expected inventory (record in commit message later):
- `_BRIDGE_STARTED_AT` (module-level float, line 420)
- `_send_response` (helper for sending Telegram messages)
- `get_session_id` / `set_session_id` (session persistence)
- `chat_projects` accessor functions
- `running_processes` registry
- `keep_typing` helper

- [ ] **Step 2: Write a smoke test that exercises the new runtime module**

Create `tests/test_patchbay_runtime.py`:

```python
"""Smoke test: patchbay.runtime exposes the symbols command handlers depend on."""

import patchbay.runtime as runtime


def test_runtime_exposes_bridge_started_at():
    assert isinstance(runtime.BRIDGE_STARTED_AT, float)


def test_runtime_exposes_send_response():
    assert callable(runtime.send_response)


def test_runtime_exposes_session_helpers():
    assert callable(runtime.get_session_id)
    assert callable(runtime.set_session_id)


def test_runtime_exposes_running_processes():
    # Should be a dict-like registry keyed by session_key
    assert hasattr(runtime, "running_processes")
    assert hasattr(runtime.running_processes, "__getitem__")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_patchbay_runtime.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'patchbay.runtime'`.

- [ ] **Step 4: Create `patchbay/runtime.py`**

Move the symbols identified in Step 1 from `bridge.py` to `patchbay/runtime.py`. For each:
- Cut the definition from `bridge.py`.
- Paste into `patchbay/runtime.py`.
- In `bridge.py`, add `from patchbay.runtime import <name>` near the other patchbay imports.
- Rename `_BRIDGE_STARTED_AT` → `BRIDGE_STARTED_AT` (drop leading underscore — it's now a public cross-module symbol).
- Rename `_send_response` → `send_response` for the same reason.

`patchbay/runtime.py` skeleton:

```python
"""Shared bridge runtime state and helpers used by command handlers.

This module owns process-wide state that must be visible to both bridge.py's
message dispatcher and the per-command handlers in patchbay/commands/. Kept
separate from bridge.py to break the cycle: commands import from runtime,
bridge imports from runtime; neither imports from the other.
"""

from __future__ import annotations

import time
from typing import Any

# Lifecycle
BRIDGE_STARTED_AT: float = time.time()

# Process registry: session_key -> subprocess.Popen | claude_sdk client | etc.
running_processes: dict[str, Any] = {}

# Session helpers — re-exported from patchbay.sessions for convenience
from patchbay.sessions import get_session_id, set_session_id  # noqa: E402

# Telegram send helper — defined here because it interleaves chunking + markdown
# downgrade (see patchbay/text_split.py) with bridge-specific photo + file sentinel
# handling. Moved from bridge.py:_send_response.
async def send_response(...) -> None:
    """Send a (possibly chunked, possibly markdown-downgraded) response to Telegram."""
    # body cut from bridge.py
```

- [ ] **Step 5: Run the runtime smoke test**

Run: `uv run pytest tests/test_patchbay_runtime.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: ≥ 785 passed. Any failure here means a `bridge.py` reference to a moved symbol still uses the old underscored name — fix and re-run.

- [ ] **Step 7: Smoke-import bridge**

Run: `uv run python -c "import bridge; print('ok')"`
Expected: `ok`. (No ImportError, no recursion.)

- [ ] **Step 8: Run the bridge smoke validate script**

Run: `uv run python validate.py`
Expected: validation passes.

- [ ] **Step 9: Commit**

```bash
git add patchbay/runtime.py bridge.py tests/test_patchbay_runtime.py
git commit -m "refactor(patchbay): extract shared runtime helpers into patchbay/runtime.py

Pulls bridge module-globals consumed by command handlers (BRIDGE_STARTED_AT,
running_processes, send_response, session helpers) into patchbay/runtime.py.
Prerequisite for splitting cmd_* handlers into patchbay/commands/ without
introducing a circular import bridge -> commands -> bridge."
```

---

## Task 4: Move lifecycle commands to `patchbay/commands/lifecycle.py`

Six handlers: `cmd_start`, `cmd_clearnew`, `cmd_kill`, `cmd_cancel`, `cmd_restart`, `cmd_ping`.

**Files:**
- Create: `patchbay/commands/__init__.py`
- Create: `patchbay/commands/lifecycle.py`
- Modify: `bridge.py` — remove the 6 handler definitions and their `CommandHandler` registrations; replace with `register_handlers(app)` call.

- [ ] **Step 1: Write a smoke test for the new module**

Create `tests/test_commands_lifecycle.py`:

```python
"""Smoke: lifecycle commands import and have the right signatures."""

from telegram.ext import CommandHandler

from patchbay.commands import lifecycle


def test_lifecycle_exports_all_handlers():
    expected = {"cmd_start", "cmd_clearnew", "cmd_kill", "cmd_cancel",
                "cmd_restart", "cmd_ping"}
    assert expected.issubset(set(dir(lifecycle)))


def test_handlers_are_async():
    import inspect
    for name in ["cmd_start", "cmd_clearnew", "cmd_kill", "cmd_cancel",
                 "cmd_restart", "cmd_ping"]:
        fn = getattr(lifecycle, name)
        assert inspect.iscoroutinefunction(fn), f"{name} must be async"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_commands_lifecycle.py -v`
Expected: FAIL — `patchbay.commands.lifecycle` does not exist.

- [ ] **Step 3: Create `patchbay/commands/__init__.py`**

```python
"""Telegram command handlers, grouped by concern.

Each submodule exports `cmd_*` async handlers; `register_handlers` wires them
all onto a telegram.ext.Application instance. Called once from bridge.main().
"""

from telegram.ext import Application, CommandHandler

from patchbay.commands import lifecycle


def register_handlers(app: Application) -> None:
    """Wire every cmd_* handler onto the Application."""
    # Lifecycle
    app.add_handler(CommandHandler("start", lifecycle.cmd_start))
    app.add_handler(CommandHandler("clearnew", lifecycle.cmd_clearnew))
    app.add_handler(CommandHandler("kill", lifecycle.cmd_kill))
    app.add_handler(CommandHandler("cancel", lifecycle.cmd_cancel))
    app.add_handler(CommandHandler("restart", lifecycle.cmd_restart))
    app.add_handler(CommandHandler("ping", lifecycle.cmd_ping))
    # (Other groups added in Tasks 5–7.)
```

- [ ] **Step 4: Move the 6 handlers from `bridge.py` to `patchbay/commands/lifecycle.py`**

For each of `cmd_start`, `cmd_clearnew`, `cmd_kill`, `cmd_cancel`, `cmd_restart`, `cmd_ping`:
- Cut the entire `async def` block from `bridge.py`.
- Paste into `patchbay/commands/lifecycle.py`.
- At the top of `lifecycle.py`, add imports:
  ```python
  from telegram import Update
  from telegram.ext import ContextTypes

  from patchbay.runtime import (
      BRIDGE_STARTED_AT,
      running_processes,
      send_response,
      get_session_id,
      set_session_id,
  )
  # Add other patchbay imports the cut handlers reference
  ```
- In `bridge.py`, delete the matching `app.add_handler(CommandHandler("<name>", cmd_<name>))` lines.

- [ ] **Step 5: Add `register_handlers(app)` call in `bridge.py:main()`**

Replace the deleted handler registrations with a single line:

```python
from patchbay.commands import register_handlers
register_handlers(app)
```

- [ ] **Step 6: Run lifecycle smoke test**

Run: `uv run pytest tests/test_commands_lifecycle.py -v`
Expected: all 2 tests PASS.

- [ ] **Step 7: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: ≥ 787 passed. Pay attention to any test that imports `from bridge import cmd_start` style — those need updating to `from patchbay.commands.lifecycle import cmd_start`.

- [ ] **Step 8: Smoke import + validate**

Run: `uv run python -c "import bridge; print('ok')" && uv run python validate.py`
Expected: both ok.

- [ ] **Step 9: Verify the bridge actually starts**

Run: `uv run python bridge.py --dry-run 2>&1 | head -20` (if `--dry-run` exists; otherwise start it briefly with a timeout):
```bash
timeout 8 uv run python bridge.py 2>&1 | tail -20 || true
```
Expected: handler-registration logs appear, no AttributeError on `cmd_start`.

- [ ] **Step 10: Commit**

```bash
git add patchbay/commands/__init__.py patchbay/commands/lifecycle.py bridge.py tests/test_commands_lifecycle.py
git commit -m "refactor(patchbay): move lifecycle commands to patchbay/commands/lifecycle.py

cmd_start, cmd_clearnew, cmd_kill, cmd_cancel, cmd_restart, cmd_ping move
out of bridge.py into a focused module. bridge.main() now calls
patchbay.commands.register_handlers(app) instead of inlining 18 add_handler
calls. First slice — Tasks 5–7 cover the remaining 12 commands."
```

---

## Task 5: Move project commands to `patchbay/commands/project.py`

Six handlers: `cmd_setproject`, `cmd_project`, `cmd_model`, `cmd_effort`, `cmd_harness`, `cmd_remote_control`.

**Files:**
- Create: `patchbay/commands/project.py`
- Modify: `patchbay/commands/__init__.py` — import + register
- Modify: `bridge.py` — remove the 6 handlers + their registrations
- Modify: `tests/test_cmd_setproject_keyboard.py` (renamed in Task 2) — update its `patch.object(bridge_mod, "_get_all_projects", ...)` to target the new module

- [ ] **Step 1: Write smoke test**

Create `tests/test_commands_project.py`:

```python
"""Smoke: project commands import and have the right signatures."""

import inspect
from patchbay.commands import project


def test_project_module_exports_handlers():
    for name in ["cmd_setproject", "cmd_project", "cmd_model", "cmd_effort",
                 "cmd_harness", "cmd_remote_control"]:
        fn = getattr(project, name, None)
        assert fn is not None, f"{name} missing from patchbay.commands.project"
        assert inspect.iscoroutinefunction(fn), f"{name} must be async"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_commands_project.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Move the handlers**

Same procedure as Task 4 Step 4. Cut from `bridge.py`, paste into `patchbay/commands/project.py`, add the imports the handlers need (likely include `patchbay.projects.{get_project_for_chat, set_project_for_chat, _get_all_projects}` and patchbay-runtime helpers).

- [ ] **Step 4: Update test_cmd_setproject_keyboard.py**

The fixture currently patches `bridge_mod._get_all_projects`. Rewire to patch the new home:

```python
with patch("patchbay.commands.project._get_all_projects", return_value=projects):
    asyncio.run(project_mod.cmd_setproject(update, ctx))
```

The fixture changes from `bridge_mod` to importing the project command module directly:

```python
@pytest.fixture()
def project_mod():
    from patchbay.commands import project
    return project
```

(Drops the heavy bridge subprocess shim — much simpler.)

- [ ] **Step 5: Wire registrations**

Add to `patchbay/commands/__init__.py`:

```python
from patchbay.commands import project as _project

# Inside register_handlers:
app.add_handler(CommandHandler("setproject", _project.cmd_setproject))
app.add_handler(CommandHandler("project",    _project.cmd_project))
app.add_handler(CommandHandler("model",      _project.cmd_model))
app.add_handler(CommandHandler("effort",     _project.cmd_effort))
app.add_handler(CommandHandler("harness",    _project.cmd_harness))
app.add_handler(CommandHandler("remote_control", _project.cmd_remote_control))
```

Delete the matching lines from `bridge.py`.

- [ ] **Step 6: Run the project tests + full suite**

```bash
uv run pytest tests/test_commands_project.py tests/test_cmd_setproject_keyboard.py -v
uv run pytest tests/ -q
```
Expected: all green.

- [ ] **Step 7: Smoke import**

Run: `uv run python -c "import bridge" && uv run python validate.py`
Expected: ok, ok.

- [ ] **Step 8: Commit**

```bash
git add patchbay/commands/project.py patchbay/commands/__init__.py bridge.py \
        tests/test_commands_project.py tests/test_cmd_setproject_keyboard.py
git commit -m "refactor(patchbay): move project commands to patchbay/commands/project.py

cmd_setproject, cmd_project, cmd_model, cmd_effort, cmd_harness,
cmd_remote_control move out of bridge.py. The keyboard test no longer
needs the heavy bridge subprocess shim — patches the new module path
directly."
```

---

## Task 6: Move observability commands to `patchbay/commands/observability.py`

Four handlers: `cmd_health`, `cmd_activity`, `cmd_soak`, `cmd_usage`.

**Files:**
- Create: `patchbay/commands/observability.py`
- Modify: `patchbay/commands/__init__.py`
- Modify: `bridge.py`

- [ ] **Step 1: Write smoke test**

Create `tests/test_commands_observability.py`:

```python
import inspect
from patchbay.commands import observability


def test_observability_exports_handlers():
    for name in ["cmd_health", "cmd_activity", "cmd_soak", "cmd_usage"]:
        fn = getattr(observability, name, None)
        assert fn is not None
        assert inspect.iscoroutinefunction(fn)
```

- [ ] **Step 2: Run test, verify FAIL**

Run: `uv run pytest tests/test_commands_observability.py -v`
Expected: FAIL.

- [ ] **Step 3: Move the handlers**

Same procedure as Tasks 4–5. Imports likely include `patchbay.activity`, `patchbay.config.ACTIVITY_LOG`, `psutil`, `subprocess` for ccusage, etc.

- [ ] **Step 4: Wire registrations**

Add to `patchbay/commands/__init__.py:register_handlers`:

```python
from patchbay.commands import observability as _obs
app.add_handler(CommandHandler("health",   _obs.cmd_health))
app.add_handler(CommandHandler("activity", _obs.cmd_activity))
app.add_handler(CommandHandler("soak",     _obs.cmd_soak))
app.add_handler(CommandHandler("usage",    _obs.cmd_usage))
```

Delete matching lines from `bridge.py`.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_commands_observability.py tests/test_health_command.py tests/test_cmd_activity.py tests/test_harness_soak.py -v
uv run pytest tests/ -q
```
Expected: all green. The existing `test_health_command.py`, `test_cmd_activity.py`, `test_harness_soak.py` may need their import paths updated from `from bridge import cmd_health` to `from patchbay.commands.observability import cmd_health`.

- [ ] **Step 6: Smoke import**

Run: `uv run python -c "import bridge" && uv run python validate.py`
Expected: ok.

- [ ] **Step 7: Commit**

```bash
git add patchbay/commands/observability.py patchbay/commands/__init__.py bridge.py \
        tests/test_commands_observability.py
# Also include any test files that needed import-path updates:
git add -u tests/
git commit -m "refactor(patchbay): move observability commands to patchbay/commands/observability.py

cmd_health, cmd_activity, cmd_soak, cmd_usage move out of bridge.py.
Existing observability tests rewired to import from the new module."
```

---

## Task 7: Move context/compact commands to `patchbay/commands/context.py`

Two handlers: `cmd_context`, `cmd_compact`.

**Files:**
- Create: `patchbay/commands/context.py`
- Modify: `patchbay/commands/__init__.py`
- Modify: `bridge.py`

- [ ] **Step 1: Write smoke test**

Create `tests/test_commands_context.py`:

```python
import inspect
from patchbay.commands import context as ctx_mod


def test_context_exports_handlers():
    for name in ["cmd_context", "cmd_compact"]:
        fn = getattr(ctx_mod, name, None)
        assert fn is not None
        assert inspect.iscoroutinefunction(fn)
```

- [ ] **Step 2: Run test, verify FAIL**

Run: `uv run pytest tests/test_commands_context.py -v`
Expected: FAIL.

- [ ] **Step 3: Move the handlers**

Same procedure. `cmd_compact` carries the largest internal helper (`_SUMMARIZE_PROMPT` constant at `bridge.py:2694`) — move that constant alongside the handler.

- [ ] **Step 4: Wire registrations**

Add to `patchbay/commands/__init__.py`:

```python
from patchbay.commands import context as _ctx
app.add_handler(CommandHandler("context", _ctx.cmd_context))
app.add_handler(CommandHandler("compact", _ctx.cmd_compact))
```

Delete matching lines from `bridge.py`.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_commands_context.py tests/test_harness_context_compact.py -v
uv run pytest tests/ -q
```
Expected: all green. `test_harness_context_compact.py` may need import path updates.

- [ ] **Step 6: Verify bridge.py is now under target**

Run: `wc -l bridge.py`
Expected: under 2,200 lines.

- [ ] **Step 7: Verify no `cmd_*` defs remain in bridge.py**

Run: `grep -nE "^async def cmd_" bridge.py`
Expected: no matches.

- [ ] **Step 8: Final smoke + lint**

```bash
uv run python -c "import bridge" && \
uv run python validate.py && \
uv run ruff check . && \
uv run pytest tests/ -q
```
Expected: all four green.

- [ ] **Step 9: Commit**

```bash
git add patchbay/commands/context.py patchbay/commands/__init__.py bridge.py \
        tests/test_commands_context.py
git add -u tests/  # any other test import-path updates
git commit -m "refactor(patchbay): move context/compact commands to patchbay/commands/context.py

Final slice — cmd_context, cmd_compact + the _SUMMARIZE_PROMPT constant
move out of bridge.py. bridge.py is now under 2,200 lines (was 3,295)
with all 18 cmd_* handlers living in patchbay/commands/{lifecycle,project,
observability,context}.py."
```

---

## Task 8 (optional follow-up): Restart the live bridge with the refactor

Only run this once Tasks 0–7 are all green. The launchd plist auto-respawns on `/restart`.

- [ ] **Step 1: Trigger drain restart over Telegram**

Send `/restart` from any allowed Telegram chat.
Expected: drain mode kicks in, bridge exits non-zero, launchd respawns within ~5 seconds, the topic gets a "bridge restarted" ping.

- [ ] **Step 2: Verify the new bridge is on the refactored code**

```bash
ps -ef | grep "bridge.py" | grep -v grep
launchctl print gui/$(id -u)/com.synodic.patchbay-relay | head -30
```
Expected: a fresh PID, started within the last minute.

- [ ] **Step 3: Send a smoke test message in any topic**

Send any short prompt. Expected: response comes back through the same MOP path, no regression.

---

## Self-review notes

- **Spec coverage:** Adrien's three threads — split bridge.py into commands/, fix pyproject hardcoded path, move stray test files — are Tasks 4–7, Task 1, and Task 2 respectively. Task 0 lands the in-flight MOP fix that's still uncommitted (a precondition for clean refactor commits). Task 3 is the unstated prerequisite that makes the bridge.py split practical without circular imports.
- **No placeholders:** Every test file body, every cmd line, every commit message is fully written. The one place I used "..." is in the `send_response` function body in Task 3 Step 4 — that's because the actual body is a verbatim cut-and-paste from `bridge.py:_send_response` and listing it inline would balloon the plan; the engineer reading Task 3 needs to copy from bridge.py, which is the only authoritative source.
- **Type consistency:** `running_processes` is the same dict-like registry across runtime + every command module. `BRIDGE_STARTED_AT` is renamed once (Task 3) and stays public from then on. `register_handlers(app: Application)` has the same signature everywhere it's referenced.
- **Parallelizability:** Tasks 1, 2, 3 touch disjoint files (pyproject.toml + new test; root test files + new tests/ files; new patchbay/runtime.py + bridge.py imports). They can run as three concurrent subagents. Tasks 4–7 must run sequentially after Task 3 because each modifies `patchbay/commands/__init__.py` and `bridge.py`.
- **Stop hook investigation:** Out of scope for this plan. The diagnostic logging is already in place (Task 0 commits it); the next failure surfaces actionable detail in `activity.jsonl`. There's nothing to actively investigate until that recurs.

---

## Coordination note (added 2026-05-08 ~04:45 UTC)

Two sessions worked on patchbay-relay tonight in parallel — author of this plan (commit `49c5164`/`fea941f`) and a sibling Telegram session.

**Done in the sibling session, not yet reflected here:**

- `MOP_RULES_DIR` set in `.env` and documented in `.env.example` (was missing entirely).
- `MOP_VERBOSE` env var + `patchbay/mop_verbose.py` (wraps MOP MCP server; surfaces every verdict to chat — `accepted`/`rewritten`/`rejected`/`failed-open` markers, with original text on rejections). Wired through `claude_sdk_mop.build_options`.
- Diagnostic logging added: `claude_sdk_mop.stop_hook_callback` logs BLOCK/ALLOW + `sent_this_turn` + `stop_hook_active`; `mop_deliver.deliver` logs entry/ok/exception with chat + text_len.
- **Plain-text safety net** in `bridge.py` v2 dispatch: collects `AssistantMessage.TextBlock` content during the turn; if `mop._patchbay_deliver.delivery_count == 0` after the loop, returns the joined plain text so `_send_response` sends it. Activity log gets `mop_delivery_count` + `fallback` fields. Eliminates the silent black hole when MOP doesn't deliver.
- `claude_sdk.py:393` `subtype=success but is_error=True` path: when `full_text` is empty, replaced cryptic `(no parseable response)` with a plain-English message naming the Anthropic API hiccup and telling the user to retry.
- Regression test `test_run_claude_cc_sdk_mop_v2_sets_resume_when_session_exists` added to `tests/test_claude_sdk_mop.py` covering the resume bug your `fea941f` fixed.

**Untracked file flagged:** `tests/test_patchbay_runtime.py` (your TDD placeholder for Task 3) currently fails on `pytest tests/` with `ModuleNotFoundError: No module named 'patchbay.runtime'`. Pre-push will fail until either the module is created (your Task 3) or the test file is gitignored/deleted. Sibling ran tests with `--ignore=tests/test_patchbay_runtime.py` to stay green in the meantime.

**Status of plan tasks:** Task 0 (MOP fix commits) — partially landed via `fea941f`; the verbose-mode + safety-net + error-message work is uncommitted in this worktree. Tasks 1–7 unstarted.
