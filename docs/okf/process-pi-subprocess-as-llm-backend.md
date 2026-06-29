---
type: process
title: pi subprocess as LLM backend
kind: decision
tags: [pi, litellm, subprocess, LLM]
confidence: 1.0
---
Replaced direct litellm calls with `pi --provider litellm --model small --print --mode json` subprocess. Pi provides session persistence, tool execution, and filesystem context per project directory.
