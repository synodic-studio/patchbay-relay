# Stargate Improvement Plan

*Author: Claude Opus 4.7 · Review date: 2026-04-21*
*Status update: 2026-04-24 — see "Where We Are Now" below for what's landed.*
*Previous work: Claude Sonnet 4.6. Branch: `develop`, 18 uncommitted files.*

---

## Where We Are Now (2026-04-24)

This audit was written before a heavy push of fixes. As of develop @ 6f44154
the file is **455 tests passing, ruff clean, validate clean**. Below is
what's landed and what's still open. Full audit detail follows unchanged.

### Landed since audit

- **Stall detector slack** (Blocker §1, audit §1a interim): `STALL_TIMEOUT`
  raised to 2400s (40 min). Real event-cadence detector is still future
  work. (commit `e6e79a6`)
- **Single-instance guard** (Blocker §2, CTB-72m): PID lockfile blocks two
  bridges from racing on `getUpdates`. (`5e8b2b6`, `e6e79a6`)
- **Atomic writes everywhere** (Critical §4): `atomic_write_text` used by
  sessions, projects, efforts, outbound. (`e6e79a6`)
- **Debounce race closed** (Critical §5, CTB-ucw): per-session `asyncio.Lock`
  via the new `SessionState` dataclass. Concurrent claims serialize.
  Regression test gathers 10 simultaneous claims and asserts exactly one
  wins. (CTB-ucw branch, fully merged)
- **SessionState consolidation** (Architecture §4c, CTB-ucw): six parallel
  dicts collapsed onto one dataclass keyed by `session_key`.
- **outbound.py file-locked** (Critical §6): `fcntl` advisory lock on
  read-modify-write prune. (`e6e79a6`)
- **Pending retry counter + give-up archive** (High §9, CTB-ucw): pending
  files persist with `attempts` count, archive to `pending/failed/` after
  3 retries with a "giving up" Telegram message.
- **`get_session_id` malformed-JSON tolerance** (High §10): catches
  `KeyError`/`TypeError`/`OSError` and quarantines instead of poisoning
  the chat. Already in current code.
- **`keep_typing` per-error handling** (High §8): `Forbidden` /
  `ChatMigrated` give up immediately, `RetryAfter` honors backoff,
  generic errors capped at 5 consecutive failures. Three previously
  silent `except Exception: pass` sites in startup/shutdown now log.
  (`5da234c`)
- **`/health` command** (Architecture §4d, CTB-3ck): uptime, active
  sessions, session/pending file counts, failed-pending archive count,
  free disk. (`d20d95d`)
- **Env-var validation at startup** (CTB-apy): every int-from-env reads
  through `_env_int` with min-value checks; missing `WORKING_DIR` /
  `PA_PLUGIN_DIR` is a fatal-with-clear-message; `CLAUDE_PATH` self-heals
  through known fallbacks. Nothing is swallowed. (`7f73ab2`)
- **Log rotation** (CTB-r7z): `bridge.err` and `bridge.log` rotate at
  startup if over 10 MiB; older archives gzip; oldest dropped past keep=5;
  `sys.stderr` and `sys.stdout` re-pointed via `dup2` so launchd's
  exec-time redirect doesn't keep us writing into the renamed inode.
  (`208a9b2`)
- **Empty-success summary retry** (new pathology, no audit §): when
  `claude -p` exits cleanly with no final assistant text the bridge
  re-invokes once with `--resume <id> --max-turns 5` and a "summarize what
  you just did" prompt, returning the summary instead of the
  `(Completed N turns…)` placeholder. (`6f44154`)
- **System-prompt tightening** for "always close with text". (`e3d1976`)
- **Outbound audit log** (CTB-80f): every Claude→Telegram chunk recorded
  with parse mode and status. (`bbbb5d3`)

### Still open (audit items not yet addressed)

| Audit ref | Item | Notes |
|---|---|---|
| §1a (full) | Event-cadence stall detector | Replace CPU polling with stdout-event cadence. The 40-min slack is interim. |
| §11 / §5 | Channels MCP plan can't run on `claude -p` | Blocked on Agent SDK migration (§4a). |
| §12 | `max_turns=500` runaway risk | Decision 2026-04-24: don't lower until we have data. Add `turns_used` / `elapsed_ms` to `activity.jsonl` first; revisit once real distribution is known. |
| §13 | `_to_markdownv2` partial-render leaks | Open. |
| §14 | Three handler duplication | Open. The CTB-ucw helpers (`_claim_or_queue`, `_drain_next`, `_release_processing`) are a partial down-payment but the wider `_process_with_claude` extraction is still ahead. |
| §15 | auth_server reflected XSS via raw `error` | **Moot 2026-04-24** — auth_server.py and the entire auth layer deleted from the repo. See `docs/apple-auth-implementation.md`. |
| §16 | IPv6 prefix rotation locking mobile sessions | Open. |
| §17 | `cmd_remote_control` stdout-only deadlock | Open. |
| §18 | pytest-asyncio mode + drain test flake | Drain test no longer fails on develop, but the underlying flake potential (no declared mode) remains. |
| §19–21 | run.sh rollback freshness, py-version matrix, coverage claim | Hygiene. |
| §4a | Agent SDK migration | Bryan: "interested later." Channels MCP unblocks once this is done. |
| §4b | Thin `bridge.py` | Open — `bridge.py` is still the orchestrator. |
| §4d (rest) | `activity.jsonl` viewer / `/metrics` | `/health` is shipped; viewer + metrics still open. |

### Recommended next (ranked)

1. **Emit `turns_used` and `elapsed_ms` to `activity.jsonl` per `claude_invoke`.**
   One-line addition; gives us data before we touch `max_turns`.
2. **Sanitize `auth_server` error HTML** (audit §15 / §1g). One-line fix,
   small XSS-shaped risk.
3. **Phase 3 chaos test + property tests** (CTB-dnc). Hardens the work
   we just did before adding more.
4. **certifi bump** (CTB-psz). Routine security hygiene.
5. **Pluggable backends per-topic** (CTB-cyz). New territory; not urgent.
6. **Repair-agent dispatch pattern** (CTB-die). Self-healing infrastructure.
7. **Event-cadence stall detector** (audit §1a full). Bigger; needs design.
8. **Agent SDK migration** (audit §4a). Unblocks Channels MCP.

---

## 0. Audit Summary

**Current state:** 1,606-line `bridge.py` + 9 `stargate/` submodules + 414-line `auth.py` + 380-line `auth_server.py`. 330 tests pass, **1 fails** (`test_queued_messages_drained_after_processing`), **3 ruff errors** (2 unused imports, 1 **duplicate test name shadowing another test**). `validate.py` passes. Significant in-flight work: new `outbound.py` + integration in `run_claude()`, new `hooks/headless-warn.py`, stall-kill markers, MarkdownV2 rendering, `/usage` command. None of it is committed.

**Maturity verdict:** the core is less fragile than its reputation — good locking in `projects.py`, a clean `stargate/` split, thorough auth tests. But five classes of bug remain.

### Blocker (fix now)

1. **10-minute stall timeouts killing legitimate work.** `STALL_CPU_THRESHOLD = 1.0%` with `STALL_TIMEOUT = 600s`. Claude is API-bound → CPU is always near zero during thinking/tool waits. The detector cannot distinguish "waiting on API" from "hung on TCC dialog." Every long task gets reaped. (`stargate/config.py:104-110`, `bridge.py:1378-1429`)
2. **No single-instance guard — 1,138 Telegram 409 Conflict errors in bridge.log (CTB-72m, filed 2026-04-21).** Two bridge processes can poll `getUpdates` simultaneously during restart overlap; Telegram allows only one poller, the second gets 409, the bridge never backs off cleanly. Same root cause as the 04-18 outage. Needs PID lockfile + startup getMe-with-backoff + aggregated log line instead of per-event spam.
3. **Duplicate test shadows another test.** `tests/test_post_init.py:130` redefines `test_sets_bot_instance` from line 55 → the first is silently skipped by pytest. Real coverage loss.
4. **`test_queued_messages_drained_after_processing` failing** on `develop`. The queued-message drain path is broken or racy.

### Critical

4. **No atomic writes anywhere.** `sessions.json`, `chat_efforts.json`, `chat_projects.json`, `outbound/*.jsonl`, session files — all `write_text()` direct. A crash or disk-full mid-write truncates the file. (`stargate/sessions.py:54`, `stargate/efforts.py:22`, `stargate/outbound.py:47-53`)
5. **Debounce race.** `if key in _processing_sessions` (line 596) is checked before the executor starts. Two messages arriving within the same event-loop tick both pass, both enter executor, both spawn `claude --resume <same_id>` in parallel → `--resume` corruption. Three handlers repeat the same pattern (`handle_message` 596, `handle_photo` 709, `handle_document` 805). No `asyncio.Lock`.
6. **`outbound.py` TOCTOU.** Append then read-modify-write prune (`outbound.py:47-53`) loses concurrent writes. No `fcntl`.
7. **Stale `_processing_sessions` on `save_pending()` OSError.** Line 615 `add`, line 617 `save_pending` can `raise OSError`, but the exception isn't in the `try` block (line 622). The `finally` at 683 rescues it, but pending is never written and the user gets no error reply.

### High

8. **`keep_typing` bare `except`** (line 412) silently swallows every kind of error, masking Telegram API issues and making the typing indicator look alive while it's actually dead.
9. **`replay_pending` deletes the file before successful send** (`bridge.py:543`). Crash during replay = message permanently lost. The claimed rationale ("prevent crash loops") is right but the ordering is wrong — store an attempt-count instead.
10. **`get_session_id` crashes on malformed session JSON.** Only catches `JSONDecodeError`; a file missing the `last_active` key throws `KeyError` at line 44 that is *not* in the except clause above it (`except (JSONDecodeError, KeyError)` wraps the `json.loads`, not the dict access). This poisons the whole chat.
11. **Channels MCP plan, as written, cannot work with `claude -p`.** See §5.
12. **`max_turns=500`.** Combined with no timeout, a runaway tool loop can chew tokens and CPU for an hour before the broken stall detector notices.

### Medium

13. `_to_markdownv2` swallows all errors then disables markdown for the rest of that chunk only — not the whole message. Partial-markdown-partial-plain renders badly in Telegram and can leak `*` / `_` characters mid-message.
14. Three handlers (`handle_message`, `handle_photo`, `handle_document`) duplicate ~100 lines of debounce/queue/drain/finally logic each. One bug in the pattern = three bugs to fix. Needs a shared `_process_with_claude(key, prompt_builder)` helper.
15. `auth_server.py` echoes raw Apple OAuth `error` string into HTML (`:262`) — possible reflected XSS if Apple ever returns unsanitised text.
16. IPv6 prefix rotation locks sessions on every message for mobile clients (`auth.py:169-186`). No allowance for common CGNAT / IPv6 rotation.
17. `cmd_remote_control` reads stdout with a 10-second deadline, never drains stderr, can deadlock on a full pipe (`bridge.py:1224-1244`).
18. No pytest-asyncio mode declared; one test is genuinely flaky (`test_queued_messages_drained_after_processing`) because it relies on `asyncio.sleep(0)` to yield across an executor boundary.

### Low

19. `run.sh` rollback to `.bridge-known-good.py` references a file that isn't auto-refreshed.
20. No Python version pin in `pyproject.toml` beyond `>=3.11`. CI would benefit from a matrix on 3.11 / 3.12 / 3.13.
21. Coverage claim in `CLAUDE.md` ("83% overall, 100% on all stargate/ modules") is stale after the uncommitted test deletions; re-measure and update.

---

## 1. Immediate Fixes (this week)

### 1a. Replace the stall detector

Drop CPU as the signal. Use **stdout event cadence** — when `claude -p --output-format json` is running, JSON events stream continuously (tool calls, assistant deltas, results). A real hang produces zero events.

Implementation:
- Wrap `subprocess.Popen(stdout=PIPE)` in a small reader thread that updates `_proc_last_event[key] = time.time()` on every full line read.
- Stall detector compares `time.time() - _proc_last_event[key]`, not CPU.
- Default threshold: **30–40 minutes of stdout silence**, env-overridable. (Bryan: 20 min was too tight; legit long runs need headroom.)
- Keep the existing "warn Claude on next run" stall marker — that UX is good.
- Remove `STALL_CPU_THRESHOLD`, `psutil` dependency for this path.

Alternative (simpler interim): set `STALL_TIMEOUT=2400` (40 min) and rely on `/kill` + explicit `/ping` feedback. This stops the bleeding while the proper detector is built.

### 1b. Make file writes atomic

Central helper in `stargate/config.py`:

```python
def atomic_write_text(path: Path, data: str, mode: int = 0o600) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
```

Replace every `write_text()` call on persisted state: `sessions.py:54,78`, `efforts.py:22`, `projects.py` (already locked — add atomic), `outbound.py:47-53`, `auth.py` session writes.

### 1c. Fix debounce race

Introduce `_session_locks: dict[str, asyncio.Lock]` keyed by `session_key`. In each of the three handlers:

```python
lock = _session_locks.setdefault(key, asyncio.Lock())
async with lock:
    if key in _processing_sessions:
        queue...
        return
    _processing_sessions.add(key)
    pending_id = save_pending(...)
# release lock, now run executor outside the lock
```

This removes the window where two messages can both pass the check. `asyncio.Lock` is re-entrant-safe in single-event-loop Python.

### 1d. Consolidate the three handlers

Extract the shared shape to `bridge._run_session_turn(session_key, chat_id, thread_id, prompt_text, bot, *, extra_model=None)`. The three public handlers (`handle_message`, `handle_photo`, `handle_document`) become thin adapters that build `prompt_text` and delegate. Removes ~200 lines; one pattern, one test.

### 1e. Fix the test and lint errors

- `tests/test_post_init.py:130` — delete the duplicate `test_sets_bot_instance` or rename it to describe what it tests.
- `tests/test_outbound.py:9` — remove unused `OUTBOUND_DIR` import (or actually use it).
- `tests/test_outbound_integration.py:5` — remove unused `time` import.
- `tests/test_debounce.py::test_queued_messages_drained_after_processing` — add proper async-barrier (e.g. a gating `asyncio.Event` that the test awaits on before appending to the queue) instead of `asyncio.sleep(0)`.

### 1f. Tighten `get_session_id`

```python
try:
    data = json.loads(session_file.read_text())
    last_active = data["last_active"]
    session_id = data["session_id"]
except (json.JSONDecodeError, KeyError, OSError):
    session_file.unlink(missing_ok=True)
    return None
```

Apply the same hardening to `chat_projects.json`, `chat_efforts.json`, outbound entries.

### 1g. Sanitize auth_server error output

```python
safe = html.escape(error[:200])
return HTMLResponse(f"<h1>Authentication failed</h1><p>{safe}</p>", status_code=400)
```

### 1h. Commit what's working

The uncommitted work is good (MarkdownV2, `/usage`, outbound, hooks, stall markers). Land it as two commits: (a) outbound + hooks + stall markers (the Channels-adjacent work), (b) MarkdownV2 + `/usage`. Don't let it rot in a dirty tree.

---

## 1.5 Self-Healing Principle (applies to every fix below)

**Rule:** every failure path should first try to repair itself. Only ping Bryan when the failure is genuinely unfixable or when a repair attempt also failed. His global CLAUDE.md is explicit about this, and the `self-healing-errors` skill exists for exactly this pattern. Today Stargate violates it in several places — it notifies on delivery failure, on stall kills, on quota handoff, on corrupted session files — instead of trying to recover.

**Pattern to follow everywhere:**

1. **Detect.** Something failed (send timeout, malformed session JSON, 409 conflict, claude exit 137, disk full).
2. **Diagnose.** Capture enough context to describe the failure: the exception, the last N log lines, the subprocess exit code, the file path, the operation.
3. **Attempt self-repair.** Dispatch a repair agent OR run a scripted repair:
   - Corrupt `sessions/*.json` → try schema-validate → if bad, rename to `sessions/.quarantine/` and start a fresh session. Don't page.
   - 409 Conflict storm → check for duplicate PID → kill the stale one → resume. Don't page.
   - Stall detector kill → mark the session so the next run warns Claude about the headless-unsafe command (already implemented in `consume_stall_kill`). Don't page.
   - `claude -p` exit 137 (OOM) → retry once with `--max-turns 50` and a trimmed prompt. Don't page.
   - Telegram send failure after N retries → write the response to `outbound/failed/` + schedule a background re-delivery agent. Don't page.
   - Disk full → delete old `activity.jsonl` rotations, retry. Don't page.
4. **Only if self-repair fails:** page Bryan with a single concise Telegram message — what failed, what was tried, what needs manual intervention. No chatter, no status updates.

**How to dispatch a repair agent from inside the bridge:**

Write a one-shot `claude -p` (or, later, Agent SDK session) with a narrow system prompt describing the failure and allowed actions. Give it bounded tool access (read logs, kill pid, write to a quarantine dir). Time-box it. Capture its result to `activity.jsonl`. If it reports `"fixed": true`, resume normal operation; if it reports `"fixed": false`, *then* page.

This turns the bridge from "one thing breaks → Bryan notices on his phone → Bryan tells Claude to fix it" into "one thing breaks → bridge fixes it → Bryan finds out in the morning from `activity.jsonl` if he's curious."

**Don't do this for:** anything that would delete user-visible content without confirmation (messages, sessions that haven't been quarantined first, pending/*.json that still has a message in it). Self-heal ≠ hide evidence.

---

## 2. Robustness Pass

### 2a. Structured concurrency

Replace the ad-hoc `ThreadPoolExecutor(max_workers=MAX_WORKERS)` with a per-session serialization guarantee. Messages for *different* sessions should run in parallel; messages for the *same* session must serialize. Two options:

- **Per-session `asyncio.Queue`**: one consumer task per active session, lives while session has work, auto-shuts after N minutes idle.
- **Global executor + session lock** (simpler): keep the executor but gate by `_session_locks[key]`.

### 2b. Typing-task cancellation discipline

Every path that sets `stop_typing` then awaits `typing_task` should `await asyncio.wait_for(typing_task, timeout=2.0)` and swallow timeout; the current code can hang forever if `keep_typing` is wedged inside its bare-except.

### 2c. Subprocess cleanup contract

`_active_procs[session_key] = proc` must be removed in a `finally` that is **guaranteed** to run even if the `run_in_executor` call is cancelled by asyncio (e.g., bridge shutdown). Verify this; today the cleanup is inside `run_claude` at line 327 — if `run_in_executor` is cancelled mid-communicate, `proc` may leak.

### 2d. Replace `max-turns=500` with a real budget

500 turns is infinite for practical purposes. Use a wall-clock deadline instead: add `--max-time-seconds 1200` (or whatever `claude -p` supports — check `claude --help`) and keep `--max-turns 50`. Bryan's UX of "I can wait 20 minutes if needed" is better served by time than turn count.

### 2e. Pending message retry + attempt counter

In `save_pending()`, store `attempts: 0`. In `replay_pending`, increment before running. If `attempts >= 3`, archive the file to `pending/failed/` and send the user "Tried 3× to replay your message after restart; giving up. Original: <text>." No more crash loops *and* no more lost messages.

---

## 3. Testing Rigour

### 3a. Cover the drain/debounce path for real

One integration test that (a) starts handler A via a task, (b) sends handler B a second message via the real Telegram handler API, (c) asserts both replies land and in order. Not a unit test with `asyncio.sleep(0)` guesswork.

### 3b. Property tests for the JSON persistence layer

`hypothesis` is light. For each file-backed dict (`efforts`, `projects`, `sessions`, `outbound`), generate random writes + concurrent-reader scenarios and assert no corruption. Catch future atomic-write regressions.

### 3c. Kill switch for the stall detector in tests

The stall detector currently runs in `post_init` and churns during the test suite. Wire `STALL_ENABLED=false` env var; test fixtures set it.

### 3d. Coverage honesty

Re-run `pytest --cov --cov-report=term-missing` after the 1a-1h fixes and update `CLAUDE.md`. The current "83% overall" claim is not verifiable until the uncommitted test deletions are stable.

### 3e. A chaos test for `run_claude`

Feed a fake `claude` binary that emits every failure mode: partial JSON, slow drip character-by-character, stderr only, hangs for 60s, exits 0 with empty stdout, exits 137. Assert every case produces a user-visible response (not silence) and no leaked process.

---

## 4. Architecture (the "amazing" part)

### 4a. Move off `claude -p` to the Claude Agent SDK *(deferred — Bryan: "interested later")*

`claude -p` is one-shot. You re-spawn it every message, pay cold-start cost, and cannot stream mid-turn interactions. The **Claude Agent SDK (Python)** gives you a long-lived, in-process session with `async for` event streaming — which is what Stargate actually wants. Benefits:

- **No more "10-minute timeout"** — you see every event, so silence means something real.
- **Real MarkdownV2 streaming** — forward Telegram "typing" from real assistant-delta events, not a hardcoded 6-second refresh.
- **Mid-turn message injection becomes possible** (see §5), which is what `channels-mcp-plan.md` actually wants.
- **Cleaner failure modes** — Python exceptions instead of parsing stdout.

This is the single biggest maturity win available — **but parked for now.** Phases 1–2 make the `claude -p` model as robust as it can reasonably be; revisit SDK when the Channels MCP work comes back off the shelf.

### 4b. Thin `bridge.py`

Even after step 4a, `bridge.py` stays the orchestrator but should shrink to ~500 lines:

```
stargate/
├── bot.py          # Telegram handlers (currently half of bridge.py)
├── runner.py       # claude invocation (replaces run_claude)
├── lifecycle.py    # post_init / shutdown / replay
├── ratelimit.py    # quota/forge handoff (exists as quota.py)
├── commands.py     # all /cmd_* implementations
└── ...             # existing modules
```

`bridge.py` reduces to env loading + `main()` + wiring.

### 4c. One source of truth for state

Today: `_active_procs`, `_processing_sessions`, `_session_start_times`, `_queued_messages`, `_proc_last_active`, `_session_locks` are six parallel dicts keyed by `session_key`, each managed separately. Replace with a single:

```python
@dataclass
class SessionState:
    proc: subprocess.Popen | None = None
    started_at: float | None = None
    last_event_at: float | None = None
    queue: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
```

`_sessions: dict[str, SessionState]`. Every stall-detector / debounce / drain call reads one object. Easier to reason about, easier to add a web dashboard later.

### 4d. Observability

`/ping` now reports active sessions. Add:
- `/health` — disk free, session file count, pending count, last restart time, last error.
- `activity.jsonl` already exists — ship `stargate/activity_viewer.py` that reads the last N minutes and renders to Telegram.
- Optional: expose `/metrics` on localhost for a Grafana agent.

### 4e. Kill `bridge.py` from the test top-level

`test_bridge.py` and `test_cmd_setproject.py` at the project root are stragglers from before the `tests/` split. Move them in.

---

## 5. Channels MCP Plan — Technical Review

**The plan (`docs/channels-mcp-plan.md`) is ambitious and directionally right, but as written it cannot work with `claude -p`.**

### Why it can't work as drafted

1. **`claude -p` has no `--channels` flag.** The plan shows `claude -p --channels server:stargate-channel` — that is speculative syntax. Confirm against `claude --help`; I don't believe it exists.
2. **`claude/channel` capability and `notifications/claude/channel` are not MCP standard.** MCP *does* support server→client notifications (`notifications/resources/updated` etc.), but there is no canonical mechanism for a client to *inject* content into an in-flight LLM conversation mid-turn. The model decides when to call tools; tools can't interrupt the model.
3. **`claude -p` is one-shot.** The process is spawned, produces one response, exits. There is no "live in-flight session" to inject into — by design.

### What would actually work

To get mid-flight delivery, pick one:

**Option A — Migrate to Agent SDK (recommended, pairs with §4a).** The Python Agent SDK has a persistent `ClaudeSDKClient.query()` loop with interleaved input. You can `await client.send_message(...)` while a previous turn is still streaming. This is the only supported path for mid-turn injection. It also unlocks real streaming to Telegram.

**Option B — Use `claude remote-control`.** Stargate already has a `/remote_control` command. This mode supports bidirectional MCP communication over a long-lived connection. The plan's architecture is closer to this than to `claude -p`. But remote-control is still a big refactor.

**Option C — Mailbox pattern.** Keep `claude -p`, but add a `check_mailbox` MCP tool Claude is instructed to call periodically. Stargate pushes messages into the mailbox; Claude discovers them on its own cadence. Not truly mid-flight, but pragmatic and doesn't require SDK migration. Matches the current `outbound.py` pattern in reverse.

**My recommendation:** the plan should be rewritten to target the Agent SDK (Option A), couple the Channels-MCP redesign with §4a of this plan, and explicitly deprecate the `claude -p` spawn model. `outbound.py` was a reasonable interim; keep it as the outbound side of the mailbox, and the SDK migration unlocks the *inbound* side cleanly.

### Smaller issues with the plan doc

- Step 2 proposes an HTTP internal API "bound to localhost only" — add token auth even on localhost (macOS processes on the box can all hit it).
- `react(chat_id, message_id, emoji)` needs the Bot API allowlist of permitted reaction emojis; not arbitrary.
- No mention of what happens when the MCP server dies mid-session.
- No backpressure — if the user hammers messages, Claude sees N notifications mid-turn and may loop.

---

## 6. Roadmap

**Phase 1 — stop the bleeding (this week)**
§1a stall detector (bump default to 30–40 min) → single-instance PID lockfile + 409-aggregation (CTB-72m) → §1c debounce race → §1b atomic writes → §1e/1f test+lint cleanup → **wire the first two self-healers (§1.5): 409-storm auto-resolver + corrupt-session-file quarantine** → commit the clean tree.

**Phase 2 — robustness + self-healing (next)**
§2a–§2e. Extend self-healing to delivery failures, OOM retries, disk full, stall kills. Build the repair-agent dispatcher. Test suite passes with chaos harness. `pytest -x` clean on a green branch.

**Phase 3 — architecture (deferred — "interested later")**
§4a Agent SDK migration. §4b/§4c cleanup rides along with it. Rewrite Channels MCP plan (§5) against the SDK. Do this only when Phase 1+2 have had a few weeks to settle.

**Phase 4 — polish**
§4d observability, §2b typing discipline audit, property tests.

---

## TL;DR for Bryan (read this)

Two blockers: (1) 10-min stall timeout reaps healthy long runs — bump to 30–40 min now, stdout-silence later. (2) CTB-72m: no single-instance guard, restart overlap → 1,138+ 409s. Add PID lockfile. Also: non-atomic file writes poison sessions on crash. **Self-healing baked into Phase 1–2:** bridge repairs itself (quarantine corrupt files, kill stale PIDs, retry OOMs) before ever pinging you. Agent SDK migration parked per your note. One failing test, three ruff errors, one duplicate test name silently hiding another.
