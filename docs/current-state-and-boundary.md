# Current state and boundary

Reference for reactivating Patchbay Relay. The repo's own README and CLAUDE.md have drifted badly from the code; this document states what Relay actually is now, what documentation must be corrected before anything is built on it, and Relay's side of the boundary with Patchbay Voice. Written 2026-07-07 from the code, not from the stale docs.

## 1. What Relay is now

**Patchbay Relay is a Telegram-to-pi bridge: each forum topic binds to a project directory and a persistent pi session, giving a full-power coding agent (edit, test, commit, deploy, shell) driven from a phone while the workstation does the work.**

The "multi-harness transport" framing in the README is no longer true. There is one harness. `patchbay/harness/` contains only `base.py` and `pi.py`; `patchbay/harness/__init__.py` imports only `PiHarness` and its `CAPABILITIES_BY_NAME` dict has exactly one key, `"pi"`; `patchbay/config.py:200-201` sets `VALID_HARNESSES = ("pi",)` and `DEFAULT_HARNESS = "pi"`. `bridge.py` contains zero references to `cc-sdk`, `claude_sdk`, or MOP. The cc-sdk and cc-sdk-mop backends were removed.

### Load-bearing invariants

These define the product. Each exists for one reason.

1. **Full tool grant.** The bridge passes `allowed_tools=None` and disallows only the interactive-UI tools (`bridge.py:627-628`); pi runs its complete default toolset, including shell, in the topic's project directory. Reason: this is the write head. A restricted agent driven by Telegram is a different product (it is Voice with the wrong transport, see section 3).

2. **Per-topic project + session routing.** Each forum topic maps to a session key (`chat_id:thread_id`) with its own project directory (`patchbay/projects.py`, `chat_projects.json`) and a resumable pi session (pi stores per-cwd sessions; the harness passes `--session <id>`, `patchbay/harness/pi.py:_build_cmd`). Reason: multi-project work becomes tab-switching, and routing is decided once per topic instead of once per message.

3. **Crash-loop self-heal.** Three or more crashes in five minutes triggers an autonomous repair session that reads the traceback and fixes the root cause (`run.sh`, `CRASH_THRESHOLD=3`). Reason: Relay is routinely edited by an agent running through itself; a bad self-edit must not require a human at the workstation. Note: the repair session is launched via `$CLAUDE_BIN` (Claude Code CLI, `run.sh:97-108`), not pi. The turn engine and the repair engine are now different agents; see section 4.

4. **Pre-flight validation + known-good rollback.** `validate.py` runs syntax, import, and smoke checks before every start; on failure `run.sh` starts a known-good snapshot instead. Reason: a syntactically broken self-edit cannot stop the bridge from running.

5. **Single-instance self-stealing lock.** `patchbay/singleton.py` holds `.bridge.lock`; a new bridge steals a stale lock or waits out a live one. Reason: two pollers on Telegram's `getUpdates` produce endless 409s; launchd restarts must never need babysitting.

### Now vestigial given pi-only

- **The harness-agnostic abstraction has exactly one backend.** The `Harness` protocol, `HarnessCapabilities` degradation matrix, and `CAPABILITIES_BY_NAME` (`patchbay/harness/base.py`, `__init__.py`) were built so the bridge could degrade gracefully across backends. Today the capability flags mostly encode what pi cannot do: `supports_interrupt=False`, `supports_mcp=False`, `supports_effort=False` (`pi.py:73-79`), and `supports_context_query` is left at its `False` default (`base.py:53`), so `/context` has no working backend at all.
- **`/harness`** still exists as a command but has one valid value.
- **`patchbay/efforts.py`** persists a per-chat effort setting for "Claude Code's `--effort` flag" (its own docstring) that the only harness advertises it does not support. Inference: this is dead configuration surface; I have not traced every caller.
- **Mid-turn push** was a cc-sdk capability; with pi, in-flight messages take the debounce-queue path only (the path CLAUDE.md line 101 calls "legacy" is now the only path).

## 2. Drift to correct before building

Reactivation must not build on the current docs. Specific contradictions, verified against the code:

- **README status banner (line 5): "alpha, no longer actively developed... moved to Hermes."** Stale. Relay is actively developed again. This is the single most misleading line in the repo, and the Voice boundary doc (section 4 follow-up) already flags it from the other side. Fix first.
- **README multi-harness framing throughout.** The tagline ("Claude Agent SDK, the SDK with MOP output filtering, or pi", line 7), the mermaid diagram routing to cc-sdk/cc-sdk-mop (lines 40-47), the module map listing `claude_sdk.py`, `claude_sdk_channel.py`, `claude_sdk_mop.py` (lines 66-68; none exist), the "Pluggable harness" design decision (line 82), the Features bullets "Multi-harness" and "Mid-turn push (cc-sdk only)" (lines 102, 110), and the prerequisite "Claude Agent SDK (pulled in by `uv sync`)" (line 123). All describe removed code.
- **CLAUDE.md cc-sdk claims.** The "Agnostic... (cc-sdk, cc-sdk-mop, pi)" principle (line 7); the `patchbay/harness/` table row describing `claude_sdk.py`/`claude_sdk_mop.py` internals at length (line 30); the session-model in-flight push description (lines 99-101); `/harness` listing `cc-sdk` as default (line 88); `/context` "cc-sdk only today" (line 90, now "no harness at all"); `/compact`'s native cc-sdk path (line 91). CLAUDE.md also still documents `patchbay/file_send.py` as wired into "the cc-sdk / pi path... and the cc-sdk-mop path" (line 27).
- **Module maps are incomplete both directions.** Neither README nor CLAUDE.md mentions modules that exist now: `patchbay/commands/` (e.g. `observability.py`), `telegram_send.py`, `outbound.py`, `models.py` (per-chat pi model mapping via litellm aliases, default `small`), `efforts.py`, `runtime.py`, `log_filters.py`, `logrotate.py`. Whoever rewrites the docs should regenerate the map from `ls patchbay/`, not patch the old one.
- **Quota handoff is a dead-letter queue.** `patchbay/quota.py:handoff_to_forge` still writes to Forge's queue dir (`config.py:183`), and CLAUDE.md line 109 itself admits Forge has been on ice since 2026-03-15. Rate-limited turns are silently parked where nothing will ever pick them up.
- **`/usage` measures the wrong thing.** It reports Claude Code quota via `ccusage` (`patchbay/commands/observability.py`; README and CLAUDE.md line 87 both describe it that way), but the turn engine is pi over litellm, potentially on non-Anthropic providers. At best it now measures a different product's quota. Inference on user impact; the wiring is verified.
- **Internal inconsistency:** README claims 708 tests (line 98), CLAUDE.md claims 800 (line 41). At least one is stale; regenerate the number rather than arbitrating.

The correction order that matters: status banner and multi-harness framing first (they misstate what the product *is*), then the periphery (quota handoff, `/usage`, `/context`, module maps), which misstate what it *does*.

## 3. Relay's side of the Voice/Relay boundary

The canonical boundary is written in `patchbay-voice/docs/scope-and-boundary.md`, section 4, and was drafted to be adopted here. Relay adopts it as written. The operative statement:

> Patchbay Voice and Patchbay Relay are two products on one premise: your real workstation does the work, your phone is just the interface. They split on what a turn is allowed to leave behind. ... **The boundary is the side effect, not the modality.**

Relay's side, stated from this repo:

**Relay is the write head.** A Relay turn is *supposed* to change the machine: edit files, run tests and arbitrary shell, commit to working branches, deploy, provision. The full tool grant (`bridge.py:627-628`) is the point, and the safety story is built for it (self-heal, validate/rollback, known-good snapshots), because a write head must survive writing to itself.

**Both siblings now run the same engine, pi.** Relay wraps the `pi` CLI as its only harness (`patchbay/harness/pi.py`); Voice runs pi behind a locked-down twelve-tool extension (Voice ADR 0008, `pi/tools.ts`). The boundary is therefore *not* the engine and never again should be described as "Relay = Claude, Voice = pi." It is three things: the tool grant (full versus read-and-note), the modality (Telegram text versus eyes-free voice), and the side-effect profile (turn changes the machine versus turn leaves only audio and at most a Note).

**Routing rule for a feature that could land in either:** ask what the turn leaves behind. Knowledge in the user's head or a Note on disk: Voice. Any other change (files, refs on working branches, running processes, deployments, infrastructure): Relay, even if the request arrives by voice and even if the change is small. Two corollaries the Voice doc already states and Relay should honor from this side: a voice front-end for making changes is a Relay input method, not a Voice feature; and a read-only Telegram query bot is Voice with the wrong transport, not a Relay feature. Multi-threaded conversation about one project belongs to Relay's medium (Telegram topics, per Voice ADR 0004).

**The one apparent overlap, reconciled: Voice's auto-commit/push of Notes.** Voice's server deterministically commits the Notes directory to a dedicated `patchbay` side branch and pushes that one ref, user-toggled, agent-untriggerable (Voice doc section 3). That is a git write, which sounds like Relay's territory. It is not. The Voice doc's ruling is correct from Relay's side too, and Relay should not claim it: the write is fixed in path, branch, message, and trigger, so it carries none of what makes a write Relay-shaped, namely an agent choosing what changes and when. Relay's claim is precisely the complement, and it is worth holding as Relay's own fence line: **any git write whose content, target branch, message, or timing is chosen by the agent or per-request belongs to Relay.** Merging the `patchbay` branch anywhere, committing to working branches, opening PRs: Relay's job, reachable today by asking a Relay topic to do it. No code change is needed on either side; the fence is already respected by both codebases.

## 4. Verdict: settle these two first

**1. Keep multi-harness as a retained seam; correct the docs to "pi-only for now," and record why.** Owner decision (2026-07-08): multi-harness stays, it is important. It is down to one backend at the moment not by preference but by external constraint: Anthropic is removing subscription (Max) coverage for *embedded* `claude -p` / Agent SDK use, so the cc-sdk and cc-sdk-mop backends are no longer viable *inside* Relay and were cut in early July 2026 (partly also to reduce noise). pi, multi-model via litellm, is what is viable now and works well enough. So both the `Harness`/`TurnEvent` protocol AND the multi-harness *intent* stay: the protocol is the vocabulary the whole bridge speaks (parser, activity logging, error classification, tests), and pluggability remains a deliberate design goal, not a killed promise.

Two honesty fixes the docs still need. First, stop describing the removed cc-sdk/cc-sdk-mop backends as if present (they are gone). Second, be explicit about *which axis* the seam is now for: the realistic future backend is a **non-Claude agent framework**, because Claude-embedded backends are policy-blocked and pi already covers the multi-*model* axis (litellm aliases). Keep the harness seam for a different *framework*, not a different *model* — conflating the two is the trap. Rewrite README and CLAUDE.md to: "multi-harness by design, currently pi-only; cc-sdk/cc-sdk-mop removed under Anthropic's embedded-subscription policy change; protocol retained as the extension seam." That stops the next agent from either re-adding a dead Claude backend or ripping out the seam. The pieces that genuinely should be reworked (not removed) are the ones that only made sense with two *live* backends today: `/harness` as an everyday switch, `CAPABILITIES_BY_NAME` as a populated registry, `/soak`'s cross-harness comparison framing — dormant until a second harness lands, not deleted. This decision is worth its own ADR, since it is an external-constraint call that will look arbitrary later without the "why."

**2. Decide what happens to the Claude-Code-shaped operational periphery.** Three subsystems still assume the old engine: self-heal repairs via `$CLAUDE_BIN` (`run.sh`), `/usage` reports Claude Code quota via `ccusage`, and quota handoff writes to a Forge queue nothing consumes. These are not doc fixes; each needs an owner decision: keep Claude Code as the deliberate repair engine (defensible: the thing being repaired is the pi bridge, so healing with a different agent avoids a broken engine repairing itself), and either point `/usage` and rate-limit handling at what pi actually consumes or remove them. The wrong outcome is leaving them half-alive, because self-heal and quota handling are exactly the paths that only run when things are already going wrong.

**Decided and done (2026-07-08): the crash-loop autonomous-repair trigger is removed.** Investigation settled it: there were two things sharing the "self-heal" name. (a) The `run.sh` crash-loop trigger that spawned `claude --dangerously-skip-permissions -p "fix the root cause and open a PR"` on 3 crashes in 300s. (b) The in-process handlers in `self_heal.py` (`corrupt_session_json` quarantine, `claude_oom_137` retry-hint), which never call claude and are engine-agnostic. Trigger (a) was old-school: built for a high-breakage era, Claude-CLI-dependent (funding going away), and, decisively, *redundant* with the validate.py pre-flight + known-good rollback right above it (`run.sh:73-82`), which is what actually prevents a bad self-edit from bricking the bridge. So (a) is gone, replaced by crash-loop detection that saves a tail to `logs/crash-loop.log`, backs off, and relies on rollback; fix forward by hand or via pi from a Relay topic. The in-process handlers (b) stay (rename `claude_oom_137` to something engine-neutral in the doc rewrite, since it fires on any OOM exit, not just claude's).

Still open, smaller: `/usage` (ccusage) and the Forge quota handoff measure/route for an engine the bridge no longer runs on. Point them at what pi/litellm actually consumes, or remove them; do not leave them half-alive.

Everything else (module maps, test counts, command tables) is mechanical once these two are settled.
