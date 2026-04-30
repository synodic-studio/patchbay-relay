# Patchbay Rename, Execution Plan

Operational companion to [`RENAME-PATCHBAY.md`](RENAME-PATCHBAY.md). That doc is the design analysis (what changes mechanically, why); this doc is the step-by-step operational plan with the detached migration script and rollback. Written 2026-04-27 against `develop`. Adrien is flying home today; this gets executed in a session when he's back at home base with iPad/Termius reachable for emergency SSH recovery.

---

## Status as of 2026-04-27 evening (read this first)

A bunch of public-facing rebrand work landed earlier today, before the operational rename runs. Picking up cold? Here's what's already true:

- **synodic.co rebrand: COMPLETE.** Phase C below is historical, not pending.
  - `content/english/patchbay.md` is the canonical landing page (commits `559c13a` + `58a5c15` on synodic-co develop).
  - `content/english/stargate.md` is **deleted**.
  - `content/english/blog/telegram-as-dev-interface.md` is **deleted** (content folded into the patchbay.md long-form essay).
  - No Hugo aliases on patchbay.md — old URLs were never published, so no SEO fallout to worry about.
  - Production deploy to Cloudflare Pages still pending Adrien's go (separate from the rename).

- **Patchbay-branded prose throughout the public artifact.** All new content uses "Patchbay Relay" (brand) and links to `github.com/synodic-studio/patchbay-relay`. The GitHub URLs 404 until the `gh repo rename` in Phase A2 lands; that's intentional and known.

- **New code shipped that the sed sweep needs to cover** (Phase B1):
  - `cloudflare-workers/url-wrapper/` (Worker code + `wrangler.toml` + `README.md` + `package.json`). Already uses `patchbay-url-wrapper` naming, no rename needed inside, but the README references `synodic-studio/patchbay-relay` URLs and assumes the rename is live. Verify after A2.
  - `scripts/python_check.py` (CPython release monitor CLI). Uses `PATCHBAY_PYTHON_PIN`, `PATCHBAY_PYTHON_KEYWORDS`, `PATCHBAY_PYTHON_IGNORE` env vars and `~/.config/patchbay/python-ignore.json` state path. **Does NOT need a rename** because it was authored under the new naming scheme.
  - `docs/python-version-management.md` — companion doc, references `patchbay-relay` repo path; update if any path moves.
  - `tests/test_python_check.py` — 23 tests covering the script, all passing as of `79d07a2`.

- **Bead tracking in this repo.** `.beads/` is committed (issue prefix `stargate-`, sync branch `develop`). The first bead `stargate-ia8` (Python pinning artifact) closed in `79d07a2`. The bead prefix should arguably also rename to `patchbay-` post-migration, but that's cosmetic and noisy — bd doesn't support a clean prefix change without rewriting issues.jsonl. Leave the prefix alone unless it actively bothers you.

- **Total commits on this branch since the plan was originally written:** roughly four. Pre-push test suite is green (752 tests, last verified `79d07a2`).

- **What did NOT happen yet (and is what this plan still drives):**
  - GitHub repo rename (`gh repo rename patchbay-relay`).
  - Local dir rename (`~/Developer/stargate` → `~/Developer/patchbay-relay`).
  - Python package rename (`stargate/` → `patchbay/`).
  - launchd plist/label rename (`com.synodic.stargate` → `com.synodic.patchbay-relay`).
  - Auto-memory dir rename.
  - `chat_projects.json` value swap.
  - `STARGATE_*` env-var compat shim.

So: Phase A and Phase B sed sweeps still apply; Phase C is done; Phase D atomic switch + Phase E verification still apply.

---

## Goal

Rename `Stargate` → `Patchbay` (brand) / `patchbay-relay` (repo slug, local dir name) / `patchbay` (Python package) in **one sweep** with **zero permanent loss of bridge access**. The bridge is offline ~30-60 seconds during the atomic switch.

## Final names

| Surface | Name |
|---|---|
| Brand (spoken, prose) | Patchbay |
| Repo slug + local dir | `patchbay-relay` |
| Python package | `patchbay` |
| pyproject `name` | `patchbay` |
| launchd label | `com.synodic.patchbay-relay` |
| Plist filename (in repo) | `com.synodic.patchbay-relay.plist` |
| Auto-memory dir | `~/.claude/projects/-Users-bryancostanza-Developer-patchbay-relay/` |
| `chat_projects.json` value for `-1003884282041_30` | `"patchbay-relay"` |
| synodic.co page URL | `/patchbay/` (file: `content/english/patchbay.md`) |

## Critical constraint

**The session driving this migration is itself spawned by the bridge as a Claude subprocess.** `launchctl unload com.synodic.stargate` will SIGTERM the bridge AND its child Claude processes — which means the executing session dies mid-rename if it runs the unload synchronously.

**Solution:** Phase D (the dangerous unload-move-load sequence) runs as a *detached* shell script that the executing session spawns and walks away from. The bridge dies → the script keeps running → the script brings the new bridge back up. When Adrien sends his next Telegram message, the new bridge resumes the executing session and post-flight verification continues.

Detachment mechanism: spawn the script via Python `subprocess.Popen(..., start_new_session=True, close_fds=True)` so it's session-detached from the bridge process group. Backup mechanism: `nohup bash script.sh </dev/null >/tmp/log 2>&1 &` works on macOS too, but the Python form is more robust.

## Pre-flight checks (run all, abort on any miss)

- [ ] `cd ~/Developer/stargate && git status` — clean.
- [ ] `cd ~/Developer/synodic-co && git status` — clean.
- [ ] `pgrep -af bridge.py` — exactly 3 pids (main + 2 workers); record them.
- [ ] `launchctl list | grep com.synodic.stargate` — exactly one entry.
- [ ] `gh auth status` — authenticated, scopes include `repo`.
- [ ] `ssh -o ConnectTimeout=5 root@bajor 'echo ok'` — Mac reachable from this session via Tailscale (will be the recovery path if migration fails).
- [ ] `df -h ~/Developer ~/Library/LaunchAgents ~/.claude` — all have ≥ 1GB free for backups.
- [ ] No active Claude turns: `cat ~/Developer/stargate/sessions/*.json | jq .state` returns no `"running"` (or wait for them to drain).
- [ ] iPad has Termius (or any SSH client) configured for `bajor` — recovery channel if everything else fails.

## Backups (taken before any destructive step)

`/tmp/patchbay-migrate-backup-<timestamp>/` containing:

- Full tarball of `~/Developer/stargate/` (excluding `.venv/`, `aider-history/`, `logs/` — too big and easily regenerable).
- `~/.claude/projects/-Users-bryancostanza-Developer-stargate/` (entire dir, includes MEMORY.md and all feedback files).
- `~/Library/LaunchAgents/com.synodic.stargate.plist` (the live installed copy).
- A snapshot of `chat_projects.json` content.
- A `STATE.txt` describing the pre-migration state and the rollback procedure.

## Phase A — While bridge is up (no impact)

A1. Take backups (above).

A2. Rename GitHub repo via `gh`:

```bash
gh repo rename patchbay-relay --repo synodic-studio/stargate
```

GitHub auto-redirects all `synodic-studio/stargate` URLs (clone, push, web). Local pushes keep working until we update the remote URL.

A3. Update local git remote URL:

```bash
cd ~/Developer/stargate
git remote set-url origin git@github.com:synodic-studio/patchbay-relay.git
```

## Phase B — Edit code in place (bridge has cached imports, no reload)

The running bridge process has its `stargate.X` modules already imported and held in memory. Changing the on-disk source has no effect on the running process. The new code only takes effect after restart in Phase D.

B1. Sed pass: `Stargate` → `Patchbay Relay` (brand prose) and `stargate` → `patchbay` (code identifiers, package paths) across:

- `*.py` (excluding `.venv/`, `aider-history/`, `__pycache__/`)
- `*.toml`
- `*.md` (README, CLAUDE.md, all of `docs/`)
- `*.plist`
- `*.sh`
- `*.js` and `*.json` inside `cloudflare-workers/` (already mostly correct, verify)
- Test fixtures under `tests/` that hardcode `stargate` paths

Caveats during sed:

- Skip `STARGATE_*` env-var names (handled separately in B4 with the compat shim, see RENAME-PATCHBAY.md §"Compat strategy").
- Skip `.beads/issues.jsonl` and `.beads/metadata.json` — bead IDs are immutable and the JSONL stores historical context. Renaming them mid-stream confuses bd's daemon.
- Skip the `archive/` paths and any external-domain references that legitimately point at the old project name (e.g. anyone's old links if they appeared in commit messages).
- Skip log content patterns and JSON keys that are operationally significant.
- Hand-verify each `*.toml` and `*.plist` after the sed.
- Spot-check `cloudflare-workers/url-wrapper/` files: most already reference `patchbay-relay` correctly, but the README has GitHub URLs that need to resolve after Phase A2 lands.
- `scripts/python_check.py` and `tests/test_python_check.py` already use `PATCHBAY_*` and `patchbay` consistently; no edits needed there beyond confirming.

B2. `cd ~/Developer/stargate && git mv stargate patchbay` — rename the Python package directory.

B3. Update `pyproject.toml`:

- `[project] name = "patchbay"` (was `stargate`).
- Any `[tool.setuptools.packages.find]` or similar — point at `patchbay`.

B4. Add the env-var compat shim in `patchbay/config.py` (per RENAME-PATCHBAY.md §"Compat strategy"). New names take precedence; legacy `STARGATE_*` reads still work and emit a one-line WARNING when consumed.

B5. Rename plist source:

- `git mv com.synodic.claude-telegram-bridge.plist com.synodic.patchbay-relay.plist`
- Update Label inside: `com.synodic.patchbay-relay`
- Update path strings inside: `/Users/bryancostanza/Developer/stargate/...` → `/Users/bryancostanza/Developer/patchbay-relay/...`

B6. Verify renames didn't break the build:

```bash
cd ~/Developer/stargate
uv run python -c "from patchbay.config import logger; print('imports OK')"
uv run pytest tests/ -q
```

If pytest fails, abort and investigate. Don't proceed to Phase D with a broken test suite.

B7. Stage + commit locally on `develop`. **Don't push yet** — push happens in Phase D after the local dir is moved.

## Phase C — synodic.co (DONE 2026-04-27)

Already shipped. Skip on re-read; left in place for the timeline.

C1-C6 superseded by:
- `559c13a` (Patchbay Relay landing page created at `content/english/patchbay.md`, old `stargate.md` deleted, blog post deleted, blog content folded into the landing page)
- `58a5c15` (dropped backward-compat aliases since old URLs were never publicly adopted)

The Tailscale preview already reflects develop. Adrien needs to green-light the production deploy separately (Cloudflare Pages), which can happen any time and does NOT need to be coordinated with the rest of this rename.

## Phase D — Atomic switch (bridge offline 30-60s, detached script)

The script lives at `~/Developer/stargate/scripts/patchbay-migrate.sh` (created in B1's pass — committed but not yet run). Spawned detached from this session.

```bash
# Spawn pattern (Python form, more robust than nohup):
python3 -c "
import subprocess
subprocess.Popen(
    ['bash', '/Users/bryancostanza/Developer/stargate/scripts/patchbay-migrate.sh'],
    start_new_session=True,
    close_fds=True,
    stdin=subprocess.DEVNULL,
    stdout=open('/tmp/patchbay-migrate.log', 'w'),
    stderr=subprocess.STDOUT,
)
"
```

Script steps (each step has explicit error handling and rolls back on failure):

D1. Write `/tmp/patchbay-migrate.state` = `started`.

D2. `launchctl unload ~/Library/LaunchAgents/com.synodic.stargate.plist`.

D3. Wait up to 30s for `pgrep -f bridge.py` to return empty. If timeout, abort.

D4. `mv ~/Developer/stargate ~/Developer/patchbay-relay` (atomic on same filesystem).

D5. `mv ~/.claude/projects/-Users-bryancostanza-Developer-stargate ~/.claude/projects/-Users-bryancostanza-Developer-patchbay-relay`.

D6. `sed -i '' 's/"stargate"/"patchbay-relay"/' ~/Developer/patchbay-relay/chat_projects.json`. Verify: `grep stargate ~/Developer/patchbay-relay/chat_projects.json` returns nothing.

D7. `cp ~/Developer/patchbay-relay/com.synodic.patchbay-relay.plist ~/Library/LaunchAgents/`.

D8. `rm ~/Library/LaunchAgents/com.synodic.stargate.plist`.

D9. `launchctl load ~/Library/LaunchAgents/com.synodic.patchbay-relay.plist`.

D10. Wait up to 30s for `pgrep -f bridge.py` to return at least 1 pid.

D11. Validate: `launchctl list | grep com.synodic.patchbay-relay` shows the new label, exit code is `0`.

D12. `cd ~/Developer/patchbay-relay && git push origin develop` — push the renamed-package commits to the renamed remote.

D13. Write `/tmp/patchbay-migrate.state` = `success`.

If any step D2-D12 returns non-zero or times out, the script enters rollback (R1-R6 below) and writes `/tmp/patchbay-migrate.state` = `failed-rolled-back` (or `failed-rollback-failed` if rollback itself errors).

## Rollback (automatic in script, also runnable manually)

R1. If new bridge running: `launchctl unload ~/Library/LaunchAgents/com.synodic.patchbay-relay.plist`.

R2. `mv ~/Developer/patchbay-relay ~/Developer/stargate` (if dir was already moved).

R3. `mv ~/.claude/projects/-Users-bryancostanza-Developer-patchbay-relay ~/.claude/projects/-Users-bryancostanza-Developer-stargate` (if memory dir was moved).

R4. Restore `chat_projects.json` from backup.

R5. `cp /tmp/patchbay-migrate-backup-<ts>/com.synodic.stargate.plist ~/Library/LaunchAgents/`.

R6. `rm -f ~/Library/LaunchAgents/com.synodic.patchbay-relay.plist`.

R7. `launchctl load ~/Library/LaunchAgents/com.synodic.stargate.plist`.

R8. Verify bridge back up: `pgrep -f bridge.py`. Write `/tmp/patchbay-migrate.state` = `rolled-back-clean`.

## Manual recovery if the script itself crashes

If `/tmp/patchbay-migrate.state` is missing or stuck at `started`/`failed-rollback-failed`, Adrien SSHs from iPad-Termius to `bajor` and runs:

```bash
bash /tmp/patchbay-migrate-backup-<latest-ts>/recover.sh
```

The recover script is generated at backup time and contains the inverse of every step that was scheduled, so it's safe to run from any partial state.

## Phase E — Post-restart verification (Adrien sends next Telegram message)

When Adrien's next message arrives, the new bridge resumes the executing session and runs:

E1. `cat /tmp/patchbay-migrate.log` — review for warnings.
E2. `cat /tmp/patchbay-migrate.state` — should be `success`.
E3. Run the 10-item validation checklist from RENAME-PATCHBAY.md §"Validation checklist post-migration".
E4. Confirm to Adrien: "All green, ready for synodic.co production deploy" (or surface any anomalies).

## What does *not* happen during migration

- No code changes beyond rename. Behavior is identical pre/post.
- No GitHub-side changes beyond the `gh repo rename` in A2. Issues, PRs, releases, stars, watchers — all preserved.
- No Telegram-side changes. Bot token, username, chat IDs, allowlist — all unchanged.
- No environment-variable changes outside the compat shim. Existing `STARGATE_*` settings keep working.

## Out of scope (handled in separate sessions)

- Renaming the Telegram bot username (cosmetic, requires BotFather flow).
- Pruning old Fanta agent memory references to "stargate" (historical, leave alone).
- Updating the `feedback_*.md` files inside the renamed memory dir to say "patchbay" instead of "stargate" (cosmetic, Claude Code keys by directory not file content).
- Renaming `com.synodic.fanta-scheduler` or any other launchd service that is unrelated.
- Anything that requires Adrien's physical Mac touch (none currently identified).

## Estimated wall-clock

| Phase | Duration |
|---|---|
| A (GitHub rename, remote update) | 1 min |
| B (sed + package rename + tests) | 5-10 min |
| C (synodic.co update + Vultr sync) | DONE, skip |
| D (detached script) | 30-60 sec offline |
| E (verification) | 2 min |

Total: ~10-15 min of executing-time once Adrien gives the go (Phase C already shipped).

## Required from Adrien before start

- Confirm dir name `patchbay-relay` (not just `patchbay`).
- Confirm the env-var compat shim approach (keep both for one cycle).
- Confirm SSH-from-iPad recovery path is set up (Termius or equivalent on `bajor`).
- Green light to start.

## Notes for the session that picks this up

- Read the **Status as of 2026-04-27** block at the top first, then this whole doc, then `RENAME-PATCHBAY.md`. The two docs are companions; this one is operational, that one is the design analysis.
- Don't re-do Phase C; it's already in production-ready state on synodic-co develop.
- The new `cloudflare-workers/url-wrapper/` and `scripts/python_check.py` directories were authored after the original plan; double-check the sed coverage in B1 includes them, but most of their content is already Patchbay-branded.
- After D12 succeeds, the `.beads/` config (sync branch, prefix) keeps working. The bead prefix `stargate-` is intentionally not renamed; bd issue IDs are content-addressable and renaming would invalidate cross-references.
- If anything in the **Status** block has drifted between when it was written and when you're reading it (commits landed, things broke, scope changed), update that block FIRST as part of your pre-flight, before touching anything else. The block is the source of truth for "what's already done."
