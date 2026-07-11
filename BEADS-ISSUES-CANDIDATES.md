# Beads Issues — Candidates Worth Doing

Curated from this repo's open/in-progress beads before beads was removed (2026-07-10).
Dropped as regenerable: legacy-test-file removal, type-hint passes, CI-gate chore. Bead
IDs kept. (Covers both CTB/patchbay and stargate namespaces.)

## Features / real bugs
- **Join Telegram messages split across multiple updates** — the client splits very long messages (~4096 chars) into separate updates; the bridge treats each fragment as its own message. Detect and reassemble into one logical message. *(stargate-2pa, stargate-4rh)*
- Telegram replies: prepend current time + time-since-last-message in the same thread (Bryan reads on mobile, timing helps). *(stargate-42n)*
- `/remote_control` is fragile — spawns `claude remote-control` as a Popen and captures first 10s of stdout; likely needs tmux or similar for a durable session. *(CTB-c2i)*
- Revisit the `max_turns=500` cap once ~2 weeks of `turns_used` data exist in `activity.jsonl`. *(CTB-sp6)*
