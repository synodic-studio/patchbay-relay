# Patchbay Rename — Execution Plan

Operational companion to [`RENAME-PATCHBAY.md`](RENAME-PATCHBAY.md). That doc is the design analysis (what changes mechanically, why); this doc is the step-by-step operational plan with the detached migration script and rollback. Written 2026-04-27 against `develop`. Adrien is flying home today; this gets executed in a session when he's back at home base with iPad/Termius reachable for emergency SSH recovery.

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

B1. Sed pass: `Stargate` → `Patchbay` and `stargate` → `patchbay` across:

- `*.py` (excluding `.venv/`, `aider-history/`)
- `*.toml`
- `*.md` (README, CLAUDE, docs/*)
- `*.plist`
- `*.sh`

Caveats during sed:

- Skip `STARGATE_*` env-var names (handled separately in B4 with the compat shim — see RENAME-PATCHBAY.md §"Compat strategy").
- Skip log content patterns and JSON keys that are operationally significant.
- Hand-verify each `*.toml` and `*.plist` after.

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

## Phase C — synodic.co (independent repo, no bridge impact)

C1. `cd ~/Developer/synodic-co && git mv content/english/stargate.md content/english/patchbay.md`

C2. Update `patchbay.md` body: replace `Stargate` → `Patchbay` in prose, update the GitHub link to `synodic-studio/patchbay-relay`, fix the meta description.

C3. Update the blog post `content/english/blog/telegram-as-dev-interface.md`: replace `Stargate` → `Patchbay` in prose, update repo cross-link.

C4. `hugo --minify -d /tmp/synodic-co-develop --baseURL "http://100.72.154.94:41829/"` — rebuild Tailscale preview.

C5. `rsync -az --delete /tmp/synodic-co-develop/ root@bajor:/tmp/synodic-co-develop/` — sync to Vultr (preview already serving on port 41829).

C6. Adrien verifies on iPad. If green, `git add` the changed files explicitly (no `git add -A`), commit, push.

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
| C (synodic.co update + Vultr sync) | 3 min |
| D (detached script) | 30-60 sec offline |
| E (verification) | 2 min |

Total: ~15-20 min once Adrien gives the go.

## Required from Adrien before start

- Confirm dir name `patchbay-relay` (not just `patchbay`).
- Confirm the env-var compat shim approach (keep both for one cycle).
- Confirm SSH-from-iPad recovery path is set up (Termius or equivalent on `bajor`).
- Green light to start.
