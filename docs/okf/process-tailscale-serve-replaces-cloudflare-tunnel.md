---
type: process
title: Tailscale serve replaces Cloudflare tunnel
kind: decision
tags: [Tailscale, networking, HTTPS]
confidence: 1.0
---
Hosted the voice-demo server via `tailscale serve --bg 8800` instead of a Cloudflare tunnel. This restricts access to the Tailscale network only, eliminating public exposure and removing ephemeral tunnel management.
