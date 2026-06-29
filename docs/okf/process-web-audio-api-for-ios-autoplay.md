---
type: process
title: Web Audio API for iOS autoplay
kind: decision
tags: [Web Audio API, iOS, Safari, autoplay]
confidence: 1.0
---
iOS Safari blocks `HTMLMediaElement.play()` after async operations, but a Web Audio API `AudioContext` unlocked on the first `touchstart` allows automatic audio playback for the rest of the session without per-play user gesture.
