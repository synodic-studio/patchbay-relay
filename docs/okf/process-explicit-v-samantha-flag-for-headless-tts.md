---
type: process
title: Explicit `-v Samantha` flag for headless TTS
kind: decision
tags: [macOS, say, TTS, audio, headless]
confidence: 1.0
---
On a headless Mac Mini, `say` without `-v` produces a ~5ms empty audio file because no GUI session owns the audio subsystem. Defaulting to `-v Samantha` bypasses this and yields real audio.
