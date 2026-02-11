#!/bin/bash
# Email triage: check both email accounts, notify Bryan via Telegram if anything important.
# Called by launchd every 25 min; random jitter makes effective interval 25-35 min.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/.env"

CHAT_ID="${ALLOWED_USER_IDS%%,*}"

# Random jitter: 0-600 seconds (0-10 min)
JITTER=$((RANDOM % 600))
sleep "$JITTER"

RESPONSE=$(/opt/homebrew/bin/claude -p \
  --dangerously-skip-permissions \
  --output-format text \
  --append-system-prompt "You are a scheduled background task. Be concise. Output ONLY a summary of important emails, or output absolutely nothing if there is nothing important." \
  "Check Bryan's email for anything important. Use himalaya CLI to check iCloud (REDACTED@example.com) and Gmail MCP for REDACTED@example.com.

Important (notify):
- Email from a real person Bryan knows (not a company/service)
- Time-sensitive items (appointments, deadlines)
- Financial alerts (bank, credit card, unusual charges)
- Security alerts (login attempts, password resets he didn't initiate)
- Package delivery updates

Not important (skip silently):
- Marketing, newsletters, promos
- Automated app/service notifications (unless security)
- Social media notifications
- Terms updates, subscription confirmations
- Routine automated emails

If anything important: output a concise summary.
If nothing important: output nothing at all." 2>/dev/null || true)

# Only send to Telegram if Claude produced output
RESPONSE=$(echo "$RESPONSE" | sed '/^$/d')
if [ -n "$RESPONSE" ]; then
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="$CHAT_ID" \
    -d text="$RESPONSE" \
    -d parse_mode="Markdown" \
    > /dev/null 2>&1 || \
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="$CHAT_ID" \
    --data-urlencode "text=$RESPONSE" \
    > /dev/null 2>&1
fi
