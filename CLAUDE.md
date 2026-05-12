# Patchbay

Telegram bot bridge that routes messages to Claude Code sessions.

## Principles

- **Agnostic.** Patchbay is a transport between Telegram and *any* coding agent. New features (channels, tuning presets, memory, etc.) must work across all harnesses (cc-cli, cc-sdk, cc-sdk-mop, pi) when the underlying capability exists, and degrade gracefully where it doesn't. Don't bake claude-only assumptions into the bridge.
- **Headless.** Every workflow must be scriptable from the phone via Telegram. No GUI, no Mac-side manual steps.

## Architecture

The bridge is modularized into a `patchbay/` package with focused modules. `bridge.py` is the entrypoint that wires everything together and re-exports symbols for backward compatibility.

### Core modules

| File | Purpose |
|---|---|
| `bridge.py` | Entrypoint — Telegram handlers, command handlers, `run_claude()`, lifecycle (imports from `patchbay/`) |
| `patchbay/config.py` | All configuration: env vars, paths, constants, logging setup |
| `patchbay/sessions.py` | Session persistence, sanitization, pending message management |
| `patchbay/parser.py` | Claude CLI output parsing (JSON array, NDJSON, single-object) |
| `patchbay/quota.py` | Quota/rate-limit detection and Forge handoff |
| `patchbay/activity.py` | Structured JSON-lines activity logging |
| `patchbay/projects.py` | Chat-to-project directory mapping |
| `patchbay/self_heal.py` | Repair-agent dispatcher — corrupt-session quarantine, stale-poller signal, claude OOM retry. |
| `patchbay/file_send.py` | Outbound file attachments. Parses `[[send-file: /abs/path \| caption]]` sentinels out of response text and uploads via `sendPhoto` (image MIME) or `sendDocument` (else). 10MB photo / 50MB doc cap. Logged to `activity.jsonl` as `outbound_file_sent` / `outbound_file_failed`. |
| `patchbay/markdown_config.py` | Single source of truth for telegramify-markdown runtime config. Both `bridge.py` and the markdown tests call `configure_telegramify()`. Heading prefixes are `▸`/`▸▸`/`▸▸▸` (not MarkdownV2 special characters). A prior drift — production used `>` while tests used `""` — hid a real parse bug where `*> heading*` was rejected by Telegram as bold-containing-blockquote. The shared function prevents that drift; `tests/test_markdown_heading_regression.py` enforces the choice with the 17 historical failing inputs. |
| `patchbay/text_split.py` | Boundary-aware Telegram chunking. `_send_response` converts the whole response to MarkdownV2 once, then `split_paired_for_telegram` aligns raw + converted slices on paragraph (`\n\n`) > line > word boundaries. Each chunk's MarkdownV2 toggle entities (`*`, `_`, `__`, `~`, `\|\|`, `` ` ``, ` ``` `) are checked by `is_markdownv2_balanced`; if any chunk is unbalanced, the whole response downgrades to plain (logged as `markdown_chunk_unbalanced`). Replaces the previous naive byte-slice chunker that cut mid-`*bold*` and triggered Telegram's `BadRequest: can't find end of bold entity` rejections in long messages with bold markdown. |
| `patchbay/harness/` | Pluggable agent backends — `base.py` = protocol + TurnEvent types + `ChannelHandle`/`ChannelCapableHarness`, `claude_cli.py` = CLI subprocess backend, `claude_sdk.py` = Claude Agent SDK backend, `claude_sdk_channel.py` = long-lived `ClaudeSDKClient` wrapper for inflight-push channels (cc-sdk only — see `docs/CHANNELS-DESIGN.md`; primitive shipped, bridge wiring pending), `claude_sdk_mop.py` = cc-sdk wired with the Model Output Protocol. Provides `build_options()` only — no `run_turn`. The bridge's `run_claude` dispatch builds a `ClaudeAgentOptions` that mounts MOP as an in-process MCP server (`mcp__mop__submit_message`), registers a `Stop` hook that calls `mop.stop()` and `Block`s if rules require justification, and uses the deliver closure from `patchbay/mop_deliver.py` to send to Telegram on the bridge's main asyncio loop. The bridge pins the returned MOP instance on `SessionState.mop` for the SDK client's lifetime so the Stop-hook closure stays alive, then returns `""` so the orchestrator's `_send_response` is a no-op (MOP already delivered). `pi.py` = badlogicgames/pi multi-model coding agent. See `docs/HARNESS-DESIGN.md`. |
| `validate.py` | Pre-flight validation (syntax, imports, smoke tests for all modules) |

### Supporting files

| File | Purpose |
|---|---|
| `run.sh` | Entry point for bridge (uses `exec` to pass signals to Python) |

### Testing

820 tests across the `tests/` dir. Run with `uv run pytest tests/`. The suite includes property tests (`hypothesis`), a `claude` chaos test that materializes a fake binary across 7 failure modes, real drain/debounce integration tests synchronized via `threading.Event`, and self-heal dispatcher tests.

**Test isolation from production paths.** `tests/conftest.py` ships an autouse fixture (`_isolate_production_paths`) that monkeypatches every production filesystem path (`PENDING_DIR`, `SESSION_DIR`, `ACTIVITY_LOG`, `LOCK_FILE`, `PHOTO_DIR`, `CHAT_PROJECTS_FILE`, `RESTART_NOTIFY_FILE`, etc.) to a per-test tmp dir, across **every** module that imports the constant (`patchbay.config`, `patchbay.sessions`, `patchbay.activity`, `patchbay.singleton`, `bridge`). Without this, `pytest tests/` while the launchd bridge is live can wipe a real user's queued reply, pollute the real `activity.jsonl`, or send SIGTERM to the live bridge via `signal_other_bridge`. When adding new production-path constants, add them to `_PRODUCTION_PATH_GROUPS` in `tests/conftest.py` so every alias resolves to the same tmp path.

When patching in tests, use the actual module path (e.g., `patchbay.sessions.SESSION_DIR`, not `bridge.SESSION_DIR`) since functions in `patchbay/` reference their own module's imports.

```bash
uv run pytest tests/ -q                    # quick run
uv run pytest tests/ --cov --cov-report=term-missing  # with coverage
uv run ruff check .                         # lint
uv run python validate.py                   # pre-flight smoke tests
```

## Launchd Services

The bridge uses `KeepAlive: { SuccessfulExit: false }` so it auto-restarts on crashes but stands down cleanly during macOS shutdown/restart (SIGTERM → exit 0 → no respawn).

| Plist (source of truth) | Installed to | Label |
|---|---|---|
| `com.synodic.claude-telegram-bridge.plist` | `~/Library/LaunchAgents/com.synodic.patchbay-relay.plist` | `com.synodic.patchbay-relay` |

After editing the plist here, copy it to `~/Library/LaunchAgents/` and reload:
```bash
cp <file>.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/<file>.plist
launchctl load ~/Library/LaunchAgents/<file>.plist
```

## Telegram Commands

All commands are registered in `bridge.py` via `CommandHandler`. Commands silently drop requests from user IDs not in `ALLOWED_USER_IDS`.

| Command | Description |
|---|---|
| `/start` | Show the command list and your Telegram user ID. |
| `/clearnew` | Discard the current session ID for this topic and start a fresh one. Conversation history is lost. |
| `/setproject [path]` | Bind this topic to a project directory under `~/Developer`. Without an argument, shows an inline keyboard to pick from all subdirectories. Pass a path relative to `~/Developer` to set it directly. Session is reset on change. |
| `/project` | Show the project directory currently bound to this topic (and the agent name if one is configured). |
| `/kill` | Kill the active Claude subprocess for this topic. Session ID is preserved — the next message resumes in the same session. |
| `/restart` | Drain mode (default): blocks new messages, waits up to `RESTART_DRAIN_TIMEOUT` (default 10 min) for in-flight turns to finish so their work isn't lost, then terminates remaining processes and exits non-zero so launchd respawns. Sends a ping to this topic after the new process starts. |
| `/restart force` | Skip drain. Terminate every active turn immediately and exit. Use when the bridge itself is wedged and waiting won't help (work in progress will be lost). |
| `/remote_control` | Start `claude remote-control` in this topic's project directory, and report connection info. If one is already running, it is replaced. |
| `/remote_control stop` | Stop the running remote-control process. |
| `/ping` | Check liveness. Reports "pong" plus a list of any sessions currently running Claude, with elapsed time. Each session is labeled with its forum topic title (cached from `forum_topic_created`/`forum_topic_edited` events into `chat_projects.json` under a `title` key) — falling back to `<dir> › <agent>` (or just `<dir>` / `<agent>`) and finally the raw `chat_id_thread_id` session key. |
| `/health` | Observability snapshot: bridge uptime, active session count, session files on disk, pending messages, failed-pending (archived) count, free disk on the data dir. |
| `/activity [event] [count]` | Show recent `activity.jsonl` entries from the user's phone. Optional substring filter (e.g. `/activity self_heal`, `/activity markdown_send_failed 15`). Default 8 entries, max 25. |
| `/usage` | Show Claude Code quota via `ccusage` as two periods (active 5h block, current Mon→Mon week). Each period shows a token bar (used / cap) and a time bar (period elapsed). The 5h block cap comes from `ccusage --token-limit max`; the weekly cap is an estimate (env var `USAGE_WEEKLY_TOKEN_CAP`, default 3B, marked `(est)` in output) because Anthropic does not publish a weekly token cap for Max plans. |
| `/harness [name]` | Show or set the agent backend harness for this topic. Valid: `cc-cli` (today's default — wraps `claude -p` subprocess), `cc-sdk` (Claude Agent SDK), `cc-sdk-mop` (cc-sdk with Model Output Protocol filtering), `pi` (badlogicgames/pi multi-model coding agent), `default` (clear override and use `STARGATE_DEFAULT_HARNESS` env). Per-chat override stored in `chat_projects.json` under the `harness` key. Every `activity.jsonl` entry that touches a turn carries `harness=<effective>` and `harness_requested=<requested>` for live-soak comparison. |
| `/soak [since] [session]` | Compare harness backends from `activity.jsonl`. Buckets per-turn events by `harness=` and prints invokes / outcomes / p50/p95 duration / OOM / quota / stall counts side-by-side. `since` accepts `30m`, `24h`, `7d`. `session` filters to one `session_key`. Wraps `scripts/harness_soak.py` which is also runnable standalone (`uv run scripts/harness_soak.py --since 7d --json`). |
| `/context` | Show current context-window usage for this chat: `Context: 40.6k / 1.0M (4%)`. cc-sdk only today (cc-cli/pi/aider/opencode advertise `supports_context_query=False`). Opens a transient SDK client that resumes the chat's session and reads `client.get_context_usage()`. |
| `/compact [steering]` | Compact the running context. Optional steering text after the command. Two paths: native (cc-sdk → `client.query("/compact …")` with before/after token counts) or fallback (every other harness → run a summarize turn, clear the session, run a handoff turn whose prompt IS the summary; same semantics as `/clearnew` with the first message pre-loaded). Requires an active session. |

### Session model

- Each forum topic (or DM chat) maps to a unique session key (`chat_id:thread_id`).
- Sessions carry a Claude `--resume` ID so consecutive messages share context.
- Sessions expire automatically after 3 days of inactivity.
- If Claude is already processing a message, new messages are queued and batched into a single follow-up invocation when the current one finishes.

### Photo / image handling

Sending a photo triggers `handle_photo`: the image is downloaded, and Claude is asked to read and describe it (or respond to the caption). The file is deleted after the response.

### Quota handoff

If Claude hits a quota/rate limit, the message is handed off to Forge (`~/Developer/Fanta/agents/dev/forge/queue/`) so it can be processed in the background and the response sent back to the same topic. Note: Forge is currently on ice (moved to drafts as of 2026-03-15) — the handoff code still writes the queue file but nothing processes it until Forge is reactivated.

## Python Environment

Managed with `uv`. Run `uv sync` to install dependencies. Scripts use `uv run` — no venv activation needed.

`run.sh` resolves the `uv` binary by probing `$UV_BIN` env var → `$HOME/.local/bin/uv` → `/opt/homebrew/bin/uv` → `/usr/local/bin/uv` → `command -v uv`, then sleeps 30s before exiting if nothing matches (so launchd's KeepAlive doesn't tight-loop). On this Mac Mini, `~/.local/bin/uv` is a symlink into mise's shim chain (`~/.local/share/mise/shims/uv` → `mise` binary → pinned uv 0.11.8 from `~/.config/mise/config.toml`). That collapses every uv invocation — `run.sh`, the `mcp-agent-mail` launchd plist, interactive shells — down to the same mise-pinned binary, so macOS TCC sees one signature and approval persists. Version bumps go through `mise use -g uv@x.y.z`.

**TCC-pinned formulas.** `mise` and `python@3.13`/`python@3.14` are pinned via `brew pin` because each Homebrew upgrade changes the Cellar binary path and silently revokes every TCC permission tied to it (Full Disk Access, Calendar, Contacts). The Library agent hung mid-turn after a mise upgrade for exactly this reason. Banana re-asserts the pins on every tick (`TCC_PINNED_FORMULAS` in `~/Developer/Fanta/agents/pa/banana/heartbeat.py`). Intentional upgrades require an in-person walk to the Mac Mini to re-grant TCC after the upgrade — the workflow is documented in banana's AGENTS.md.

## Authentication

There is none. The bridge gates messages on `ALLOWED_USER_IDS` only.
