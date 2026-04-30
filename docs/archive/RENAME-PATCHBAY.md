# Renaming Stargate → Patchbay

Migration plan from "Stargate" to "Patchbay" (or whichever final name lands).
Written 2026-04-27 against `develop` at d20faf0. The other agent on this
project may have its own plan — treat this as one input, not the truth.

This is a working CLI bridge that runs as a launchd agent and holds live
state on disk. The rename has to land cleanly or one of: (a) the bridge
goes offline, (b) topic→project bindings vanish, (c) queued user replies
get orphaned, (d) cross-repo agents lose track of where stargate lives.
The biggest risks are toward the bottom; read those first if you are
under time pressure.

## What changes mechanically

### Inside the repo

| Surface | Today | After |
|---|---|---|
| Repo dir | `~/Developer/stargate/` | `~/Developer/patchbay/` |
| Python package dir | `stargate/` | `patchbay/` |
| `pyproject.toml` `[project].name` | `stargate` | `patchbay` |
| Module imports | `from stargate.config import …` | `from patchbay.config import …` |
| Top-level entry | `bridge.py` | unchanged (generic name) |
| Run script | `run.sh` | unchanged |
| Lockfile | `.bridge.lock` | unchanged |
| Activity log | `activity.jsonl` | unchanged |
| State dirs | `sessions/`, `pending/`, `aider-history/` | unchanged (move with repo) |
| Env-var prefix | `STARGATE_*` | `PATCHBAY_*` (see Compat below) |

There are ~400 `stargate`/`STARGATE` literals across `*.py`/`*.toml`/`*.plist`
files (~330 in tests, the rest in `bridge.py`/`stargate/*.py`/docs/plist).
Most are import paths and module attribute references; a few are env-var
names; a few are user-facing strings. A `git grep -i stargate` after the
rename is the right post-flight check.

### Launchd

| Surface | Today | After |
|---|---|---|
| Plist source | `com.synodic.claude-telegram-bridge.plist` (in repo) | `com.synodic.patchbay.plist` |
| Plist installed | `~/Library/LaunchAgents/com.synodic.stargate.plist` | `~/Library/LaunchAgents/com.synodic.patchbay.plist` |
| Service label | `com.synodic.stargate` | `com.synodic.patchbay` |
| `WorkingDirectory` | `/Users/bryancostanza/Developer/stargate` | `/Users/bryancostanza/Developer/patchbay` |
| `StandardOutPath` | `…/stargate/logs/bridge.log` | `…/patchbay/logs/bridge.log` |
| `StandardErrorPath` | `…/stargate/logs/bridge.err` | `…/patchbay/logs/bridge.err` |

The `Label` is the launchctl service identifier; renaming it requires
`launchctl unload` (old) followed by `launchctl load` (new). Restart-in-
place via `kickstart` is *not* sufficient.

### Telegram and external state

- The Telegram bot token, bot username, chat IDs, topic IDs, allowlist —
  none of these change. The rename is repo-side; the bot keeps polling
  Telegram with the same credentials.
- GitHub remote: `synodic-studio/stargate` → `synodic-studio/patchbay` is
  optional. GitHub auto-redirects HTTPS/SSH git URLs after a repo rename;
  pushes/pulls keep working via the redirect. Worth updating
  `.git/config` for hygiene.

## Files that move atomically with the repo dir rename

These are runtime state. They're inside `~/Developer/stargate/` today and
must end up inside `~/Developer/patchbay/` with the rename. A `git mv` on
the parent (or `mv ~/Developer/stargate ~/Developer/patchbay`) handles
all of them in one step:

- `chat_projects.json` — topic→project mapping; if this is lost, every
  topic forgets its `/setproject` binding and falls back to the working
  dir default. Survives the dir rename. **But** see "chat_projects.json
  content" below.
- `sessions/` — Claude Code session ID per topic. Loss = each topic
  starts a fresh `/clearnew` next message.
- `pending/` — queued messages that have not been delivered yet. Loss =
  any in-flight reply at rename time goes silent.
- `activity.jsonl` — observability log read by `/activity`, `/health`,
  `/soak`. Loss = those commands return empty for the historical window.
- `.bridge.lock` — singleton lockfile; should be removed before the new
  bridge starts so `acquire_singleton` doesn't read a dead pid from the
  old install. (Auto-stealing handles this anyway, but cleaner to remove.)
- `aider-history/`, `.quarantine/` — minor but inside the dir; they move
  with it.

## chat_projects.json content (the easy-to-miss one)

`chat_projects.json` maps `chat_id_thread_id` keys to project directory
names (resolved relative to `~/Developer/`). The first entry today is:

```
"-1003884282041_30": "stargate"
```

That's Adrien's stargate-development topic. After rename, this string
must become `"patchbay"`, otherwise `/setproject` resolution lands in
a directory that no longer exists and the topic breaks until manually
re-bound.

`grep -n stargate chat_projects.json` will surface this and any other
topics anyone has bound to this repo.

## Auto-memory directory (the *easy-to-forget* one)

Claude Code stores per-project memory at:

```
~/.claude/projects/-Users-bryancostanza-Developer-stargate/
```

The directory name is derived from the project path with `/` → `-`. After
the repo rename, Claude Code will look for memory under
`-Users-bryancostanza-Developer-patchbay/` and find an empty dir, losing
every saved feedback/project/user memory accumulated so far.

Mitigation: **rename or symlink the auto-memory dir as part of the same
migration step.**

```bash
mv ~/.claude/projects/-Users-bryancostanza-Developer-stargate \
   ~/.claude/projects/-Users-bryancostanza-Developer-patchbay
```

Inside the renamed dir, `MEMORY.md` and several `feedback_*.md` /
`project_*.md` files reference the project by name. Update the literal
strings (`stargate` → `patchbay`) for clarity, but Claude Code itself
keys by directory path, not file content — the rename of the dir is the
load-bearing step.

## Cross-repo references (the slow leak)

Other repos and notes hard-code `~/Developer/stargate` or mention
"Stargate" by name. Most are non-load-bearing prose; a few are
script-y enough to need updating:

- `~/.claude/CLAUDE.md` — Adrien's global Claude Code instructions. Doesn't
  reference stargate today (verified).
- `~/Developer/CLAUDE.md` — parent CLAUDE.md. Also doesn't reference
  stargate today (verified).
- `~/.claude/projects/-Users-bryancostanza-Developer-stargate/memory/*.md`
  — covered above.
- `~/.claude/projects/-Users-bryancostanza-Developer-Fanta/memory/*.md` —
  has `feedback_stargate_cleanup.md`, `project_stargate_reload_paradox.md`.
  Keep them, optionally rename + update prose. Not load-bearing.
- `~/Developer/Fanta/agents/**/*.md` — many historical references in
  `agents/dev/ernest/inbox/`, `agents/drafts/forge/memory/`, etc. These
  are agent memory/inbox archives. Don't touch them — they're history.
- Anything that imports `stargate` as a python package outside this repo
  — unlikely, but `grep -rn "from stargate\|import stargate" ~/Developer
  ~/.claude` will find it.

## Migration sequence (the load-bearing 60 seconds)

The window where the bridge is offline. Aim for under a minute.

1. **Pre-flight**: confirm no active turns. `/ping` should report no
   running sessions, or wait for them to finish. If the bridge is
   processing a long claude turn at rename time, the user gets a silent
   drop (the new bridge won't replay because PENDING_DIR moves cleanly
   with the rename, but the in-flight claude subprocess gets orphaned).
2. **Stop bridge**: `launchctl unload ~/Library/LaunchAgents/com.synodic.stargate.plist`.
   Confirm with `pgrep -f bridge.py` returning nothing.
3. **Move repo**: `mv ~/Developer/stargate ~/Developer/patchbay`. This
   moves the entire tree including state files in one syscall. Fast,
   atomic on the same filesystem.
4. **Move auto-memory**: `mv ~/.claude/projects/-Users-bryancostanza-Developer-stargate ~/.claude/projects/-Users-bryancostanza-Developer-patchbay`.
5. **Code rename**: in `~/Developer/patchbay/`, rename the package dir
   (`mv stargate patchbay`), update `pyproject.toml` `name`, run a
   sed-equivalent replace across `*.py` for the import path, update the
   plist source file. (The other agent's plan probably has the
   exact replace strategy — match theirs.)
6. **Update chat_projects.json**: replace `"stargate"` value(s) with
   `"patchbay"`.
7. **Install new plist**: `cp com.synodic.patchbay.plist ~/Library/LaunchAgents/`,
   then `launchctl load ~/Library/LaunchAgents/com.synodic.patchbay.plist`.
8. **Remove old plist**: `rm ~/Library/LaunchAgents/com.synodic.stargate.plist`.
9. **Verify**: `/ping` from a Telegram topic, `/health`, send a real message,
   confirm reply lands. `tail -f ~/Developer/patchbay/logs/bridge.err`
   for errors.

If steps 5–6 happen between 2 and 7, the bridge is offline for the whole
window. That's fine for a planned migration. Don't try to rename in
place with the bridge running — the singleton lock will fight the move.

## Biggest "everything just works" dangers

Ranked by blast radius. The first three are the ones to triple-check.

1. **Stale chat_projects.json values** (high probability, high impact).
   If `"stargate"` survives in this file, those topics route to a path
   that no longer exists. The bridge will log "project dir not found"
   on every message in those topics until manually fixed. **Mitigation:
   `grep stargate chat_projects.json` after step 6, expect zero hits.**
2. **Auto-memory dir not renamed** (high probability, medium impact).
   Claude Code silently starts with empty memory in this repo. No
   crash, just a loss of accumulated user/feedback/project memory.
   **Mitigation: step 4 above is non-optional.**
3. **Launchd label collision or orphaned plist** (medium probability,
   high impact). If the old plist is still in `~/Library/LaunchAgents/`
   and you reboot, launchd will try to start *both* the old (now-broken)
   and new agents. Result: 409 Conflict storm on Telegram getUpdates,
   self-heal will SIGTERM one, lock-file thrash. **Mitigation: step 8
   above. Verify with `launchctl list | grep synodic` showing exactly
   one entry post-migration.**
4. **`STARGATE_*` env var rename without compat shim** (medium
   probability, medium impact). Three env vars today:
   `STARGATE_DEFAULT_HARNESS`, `STARGATE_AIDER_MODEL`,
   `STARGATE_OPENCODE_MODEL`. If the launchd plist or any shell rc file
   sets these but the code reads `PATCHBAY_*`, the configs silently
   revert to defaults. **Mitigation: either keep the `STARGATE_` prefix
   (lowest-risk option — env vars don't have to match the brand), or
   add a fallback `os.environ.get("PATCHBAY_X", os.environ.get("STARGATE_X", default))`
   for one release cycle, then remove.**
5. **In-flight claude subprocess at migration time** (low probability,
   low-to-medium impact). If a user's turn is mid-flight when you
   `launchctl unload`, the claude subprocess dies as a child of the
   bridge, response is lost, pending file moves with the dir but
   on the next bridge start the *replay* runs claude *again* — not
   ideal but not catastrophic; the user gets a "Recovered after bridge
   restart" prefix. **Mitigation: pick a quiet moment, watch /ping.**
6. **The repo's own auto-memory references stargate by name** (low
   probability, very low impact). Several `feedback_*.md` files inside
   `~/.claude/projects/-Users-bryancostanza-Developer-stargate/memory/`
   say "stargate" in their prose. After step 4 they'll still load fine
   (Claude Code keys by directory, not file content), but future-me
   reading them will be briefly confused. **Mitigation: optional
   sed pass over `MEMORY.md` and the listed files. Cosmetic.**
7. **GitHub remote URL** (low probability, very low impact). After the
   GitHub repo rename (if you do that), `git push` keeps working via
   the redirect for an indefinite period, but `git remote set-url
   origin git@github.com:synodic-studio/patchbay.git` is the clean fix.
8. **Pre-push hook running tests against the wrong path** (low
   probability, low impact). The pre-push hook in this repo runs
   `uv run pytest`. If it has any hard-coded `~/Developer/stargate`
   paths… (verified clean today, but worth a `grep stargate
   .git/hooks/`).
9. **Documentation rot** (high probability, very low impact). Many
   `docs/*.md` files reference "Stargate" and `~/Developer/stargate`.
   Not load-bearing. A find/replace pass at the end is fine; doesn't
   block migration.
10. **Sessions and bridge.lock contention if both old and new are
    briefly running** (very low probability if you follow the sequence,
    catastrophic if not). Don't `launchctl load` the new plist without
    `unload`-ing the old first. Singleton + flock will fight to the
    death, you'll get 409 storms, both bridges will retry, possibly
    one will exit non-zero and respawn under launchd. **Mitigation:
    sequence in steps 2 → 7 strictly. Verify via `pgrep -af bridge.py`
    showing zero between steps 2 and 7, then exactly one after step 7.**

## Things that should *not* change

- `bridge.py` — the entrypoint module name. It's generic, it works.
- `run.sh` — same.
- `.bridge.lock` — same.
- `activity.jsonl` filename — same.
- The Telegram bot token, username, chat IDs, allowlist.
- `~/.claude/CLAUDE.md` and `~/Developer/CLAUDE.md` — neither references
  stargate today (verified).
- The Forge handoff path inside Fanta — that's Fanta's namespace,
  unaffected by this rename.
- Historical Fanta agent memory and inbox files referencing "stargate"
  — they're history. Leave them.

## Compat strategy (optional, recommended for env vars)

If you want zero downtime for any external thing that might still set
`STARGATE_*` env vars (Adrien's shell rcs, launchd plist content the
other agent might miss, third-party scripts), add a one-release-cycle
fallback:

```python
# stargate/config.py (or patchbay/config.py post-rename)
def _env_with_legacy(new_key: str, legacy_key: str, default: str) -> str:
    return os.environ.get(new_key, os.environ.get(legacy_key, default))

DEFAULT_HARNESS = _env_with_legacy("PATCHBAY_DEFAULT_HARNESS", "STARGATE_DEFAULT_HARNESS", "cc-cli")
```

Log a warning when the legacy var is read so you know when it's safe
to remove. Drop the shim after one or two restarts of confirmed clean
operation.

## Validation checklist post-migration

Run these in order. Each one should pass before moving to the next.

- [ ] `pgrep -af bridge.py` shows exactly one process, working dir is
      `~/Developer/patchbay`.
- [ ] `launchctl list | grep synodic` shows exactly one entry,
      `com.synodic.patchbay`.
- [ ] `~/Developer/stargate` does not exist.
- [ ] `~/Library/LaunchAgents/com.synodic.stargate.plist` does not exist.
- [ ] `grep -n stargate ~/Developer/patchbay/chat_projects.json` returns
      nothing.
- [ ] `cd ~/Developer/patchbay && uv run pytest tests/ -q` passes.
- [ ] `/ping` from a Telegram topic returns "pong" with bridge uptime
      counted from the migration moment.
- [ ] Send a normal message in a topic; verify reply lands within
      reasonable time.
- [ ] `/activity` returns the historical entries (proves activity.jsonl
      moved cleanly).
- [ ] `/health` reports the new working directory and reasonable
      pending/session counts.

## Open questions for the other agent

- Are we renaming the GitHub repo too, or just local? The plan above
  assumes "yes eventually, no rush — git redirect keeps things working".
- Are we renaming the Telegram bot's username? (Pure cosmetic, no
  functional impact unless yes.)
- Final name: "Patchbay" or something else? The doc uses Patchbay
  throughout; one find-replace updates this file if the name shifts.
- Compat shim for `STARGATE_*` env vars: keep one cycle, or hard cut?
  Adrien's launchd plist is the only place these are set today
  (verified — none in `~/.zshrc` or shell rcs as of this writing,
  but worth a final check).
