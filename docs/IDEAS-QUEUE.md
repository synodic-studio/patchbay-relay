# Stargate Ideas Queue

Things Adrien has flagged for future discussion. Not commitments — just stash so they don't get lost.

## Tuning presets (in design)

Per-chat verbosity / depth dial (e.g. `/preset terse`). Designed to work across all harnesses (agnostic principle) by injecting a snippet into the system prompt. Active discussion happening — see HARNESS-DESIGN.md and the channels work for context.

Five presets I'm prototyping (Adrien said "you provide ideas"):

| Preset | Shape |
|---|---|
| `terse` | One- or two-sentence replies. No headers, no examples. Best for phone use and quick check-ins. |
| `balanced` | Today's default. Short paragraphs, examples when useful. |
| `deep-dive` | Full reasoning, alternatives, tradeoffs, citations of files/lines. For audits, planning, debugging. |
| `teach-me` | Explain like new to the topic. Define jargon, link to where to learn more, show reproductions. |
| `autonomous` | Terse + actively flag blockers and risks. For background/long-running tasks where Adrien isn't watching. |

Open questions Adrien will weigh in on later:
- One-shot vs. sticky? (`/preset deep-dive` for one turn vs. for the chat forever)
- Auto-detect from prompt shape? (e.g. `?` ⇒ terse, `audit X` ⇒ deep-dive)
- Combine with `/effort`? (effort = compute budget; preset = output shape — orthogonal)

## Memory command

`/memory` similar to claude's. Per-chat or global. Lets Adrien write durable preferences ("always commit straight to develop", "Forge is on ice") that get injected into every system prompt for that chat (or globally).

Open: storage location (chat_projects.json vs. dedicated file), how memory gets surfaced when editing (read it back, vs. blind append), and whether claude can write to it via a tool.

## Going-to-bed-style skills / slash commands

Reusable Telegram commands that bundle a workflow. Examples:

- `/going-to-bed <task>` — "Ask me everything you need to know now, then work as long as you can on <task>." Bundles the question-batching + autonomous-execution pattern.
- Similar candidates: `/morning-brief`, `/audit <thing>`, `/quick-fix <thing>`.

These are essentially canned system-prompt prefixes that adjust the model's behavior for a single turn. Could live as Telegram-side commands (handled in bridge.py) or as harness-side skills (loaded via `--skill`/`--append-system-prompt`).

Decision needed: do these become first-class stargate commands, or do they live as skills that the user-side claude session loads?
