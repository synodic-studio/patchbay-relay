# Voice Demo

A press-to-talk prototype that proves the loop:

```
phone (hold button, speak, release)
  → cloudflare tunnel → Mac
    → faster-whisper  (local transcription)
    → litellm "small" (local OpenAI-compatible proxy)
    → macOS `say`     (local TTS → AAC/m4a)
  → audio streamed back, plays in the page
```

It's a stepping stone toward the patchbay-relay vision: eventually "speaking with
a repo" from the phone, and one day driving a narrowly-scoped (read-mostly,
single-file/dir write) coding agent. This prototype intentionally does **none** of
that yet — it just closes the voice round-trip.

Everything runs on the Mac in one process. The phone only needs a browser.

---

## Prerequisites (on the Mac)

1. **`uv`** — already used by patchbay-relay. `server.py` is a self-contained
   [PEP 723](https://peps.python.org/pep-0723/) script; `uv run` installs its
   deps (fastapi, faster-whisper, etc.) into an ephemeral env automatically.
2. **A litellm proxy** exposing a `small` alias, OpenAI-compatible, on
   `http://localhost:4000`. Quick sanity check:
   ```bash
   curl http://localhost:4000/v1/chat/completions \
     -H "Authorization: Bearer sk-anything" \
     -H "Content-Type: application/json" \
     -d '{"model":"small","messages":[{"role":"user","content":"hi"}]}'
   ```
   If your proxy is on another port/host or the alias is named differently, set
   `LITELLM_BASE_URL` / `LITELLM_MODEL` (see Config).
3. **`cloudflared`** for the tunnel: `brew install cloudflared`.
4. macOS itself — `say` and `afconvert` are built in (used for TTS).

---

## Run it

**Terminal 1 — the server:**
```bash
cd voice-demo
uv run server.py
```
First run downloads the whisper model (a few seconds for `base.en`).

**Terminal 2 — the tunnel:**
```bash
cloudflared tunnel --url http://localhost:8800
```
This prints a public `https://<random>.trycloudflare.com` URL.

**On your phone:** open that URL in Safari. Grant microphone access (the tunnel's
HTTPS is what makes the mic available). Hold the big button, speak, release.
You'll see your transcript, the reply text, and hear the reply.

> Optional: in Safari, **Share → Add to Home Screen** to get a full-screen,
> app-like PWA.

---

## Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `VOICE_HOST` | `127.0.0.1` | bind host |
| `VOICE_PORT` | `8800` | bind port (match the tunnel) |
| `WHISPER_MODEL` | `base.en` | faster-whisper size (`tiny.en`, `small.en`, `medium.en`, …) |
| `WHISPER_DEVICE` | `auto` | `cpu` / `cuda` / `auto` |
| `WHISPER_COMPUTE` | `int8` | compute type (`int8`, `int8_float16`, `float16`, …) |
| `LITELLM_BASE_URL` | `http://localhost:4000` | OpenAI-compatible base |
| `LITELLM_MODEL` | `small` | model / alias to call |
| `LITELLM_API_KEY` | `sk-anything` | bearer token for the proxy |
| `VOICE_SYSTEM_PROMPT` | (concise voice prompt) | system prompt |
| `TTS_VOICE` | system default | macOS `say` voice (e.g. `Samantha`; list with `say -v ?`) |

Example — faster model + a nicer voice:
```bash
WHISPER_MODEL=small.en TTS_VOICE=Samantha uv run server.py
```

---

## How it fits together

- **`server.py`** — FastAPI app. `POST /api/talk` runs the asr→llm→tts pipeline
  and returns `{transcript, reply, audio_url, timing}`. `GET /audio/<id>` serves
  the generated clip. `GET /healthz` reports config + whether the whisper model
  is loaded. Per-turn timings are printed to the server log.
- **`static/index.html`** — single-page client. `getUserMedia` + `MediaRecorder`
  capture audio on hold; on release it POSTs the blob and plays the returned
  audio. Handles the iOS Safari autoplay block with a "▶ replay" fallback button.

### Notes / limitations (it's a prototype)

- **Latency** is request/response, not token-streaming. `say` produces the whole
  clip before it's returned. Good enough to prove the loop; real-time streaming
  TTS is a later step.
- **No auth.** The trycloudflare URL is public-but-unguessable and ephemeral.
  Don't leave it running unattended. Adding a shared-secret header is the obvious
  next hardening step (mirrors patchbay-relay's `ALLOWED_USER_IDS` posture).
- **macOS only** for TTS (`say`/`afconvert`). The rest is cross-platform.
- Generated audio lives in a temp dir and is not cleaned up aggressively; it's
  throwaway.

---

## Next steps toward "speak with a repo"

1. Add a shared-secret header check (phone ↔ Mac).
2. Swap the plain litellm call for a tool-capable agent turn scoped to a repo
   with **read-only** filesystem tools first.
3. Stream TTS for lower latency.
4. Only then consider narrow write access (single file / single dir), gated.
