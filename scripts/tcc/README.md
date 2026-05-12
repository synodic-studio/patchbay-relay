# TCC tooling

macOS TCC (Transparency, Consent, and Control) keys every permission to the realpath of the binary that asked for it. When Homebrew, mise, or uv upgrades Python/Node/Claude in place, the path changes and every Full Disk Access, Apple Events, Calendar, Reminders permission tied to the old path is silently revoked. The launchd daemon hangs forever waiting on a permission dialog you cannot see — only the on-host user can click "Allow".

These three scripts close that gap.

| Script | Role |
| --- | --- |
| `approve.py` | One-shot. Triggers every common TCC prompt at once (folders + Apple Events) so you click "Allow" through them in one sitting after an upgrade. |
| `check.sh` | Pre-flight. Saves realpaths of `python3` / `node` / `claude` in `.tcc-paths`; on subsequent runs prints a warning + exits 1 if anything changed. Called from `run.sh` at host startup. |
| `watcher.py` | Long-running launchd daemon. Tails `log stream --predicate 'process == "tccd"'` and Telegram-pings you the *moment* macOS shows a TCC dialog — so you find out within seconds, not hours. 120s dedup window per binary. |

## Approve everything after an upgrade

Run with whichever Python you want TCC to remember — its realpath is what gets keyed:

```bash
/path/to/your/python3 scripts/tcc/approve.py
```

## Run the watcher as a launchd agent

1. Copy the template to LaunchAgents and rename to your reverse-DNS label:
   ```bash
   cp scripts/tcc/com.synodic.tcc-watcher.example.plist ~/Library/LaunchAgents/<your-label>.plist
   ```
2. Edit it. Replace every `REPLACE_*` placeholder. The required env vars are `TCC_WATCHER_CHAT_ID` and either `TELEGRAM_BOT_TOKEN` (inline) or `PASSWORD_STORE_DIR` (so the script can call `pass show telegram-bot-token`).
3. Load it:
   ```bash
   launchctl load ~/Library/LaunchAgents/<your-label>.plist
   ```

## Environment variables

| Var | Purpose |
| --- | --- |
| `TCC_WATCHER_CHAT_ID` | Telegram chat to alert. Required. |
| `TCC_WATCHER_THREAD_ID` | Forum topic id within that chat. Optional. |
| `TELEGRAM_BOT_TOKEN` | Bot token. Optional — falls back to `pass show telegram-bot-token`. |
| `TCC_WATCHER_CAFILE` | Override path to a PEM cert bundle. Auto-detects certifi / `/etc/ssl/cert.pem` otherwise. |
