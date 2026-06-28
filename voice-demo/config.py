from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

HOST = os.environ.get("VOICE_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOICE_PORT", "8800"))

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
WHISPER_INITIAL_PROMPT = (
    "Technical discussion about software development, AI, and coding tools. "
    "Keywords: Python, TypeScript, SwiftUI, FastAPI, Pydantic, LangChain, LangSmith, "
    "LangGraph, LlamaIndex, litellm, OpenAI, Anthropic, Claude, pi coder, pi coding agent, "
    "faster-whisper, Whisper, RAG, evals, embeddings, RLHF, HuggingFace, Transformers, "
    "PyTorch, JAX, Tailscale, PocketBase, TypeBox, Tuist, SwiftLint, SwiftFormat, "
    "Patchbay, MCP, SDK, API, CLI, JSON, NDJSON, asyncio, FastAPI, uvicorn, "
    "GitHub, git, branch, commit, diff, refactor, endpoint, middleware, harness."
)

PI_BIN = os.environ.get("PI_BIN", shutil.which("pi") or "pi")
PI_PROVIDER = os.environ.get("PI_PROVIDER", "litellm")
PI_MODEL = os.environ.get("PI_MODEL", "small")

DEVELOPER_DIR = Path(os.environ.get("DEVELOPER_DIR", os.path.expanduser("~/Developer")))
CHATS_FILE = Path(os.environ.get("CHATS_FILE", os.path.expanduser("~/.voice-demo-chats.json")))
TTS_VOICE = os.environ.get("TTS_VOICE", "Samantha").strip()

SYSTEM_PROMPT = """You are a voice coding assistant accessed from a mobile phone. The user speaks to you and your replies are read aloud by text-to-speech. Follow these rules strictly at all times:

Speak in plain English only. Never use markdown, headings, bullet points, numbered lists, code blocks, backticks, bold, italics, URLs, or any other formatting meant for visual reading. Write exactly as you would speak to someone on a phone call.

Keep answers short and conversational. One to three sentences unless the user clearly needs more. When referring to code, describe it in plain words rather than quoting syntax.

Compose your entire reply before delivering it. Give one complete spoken response per turn — not a series of chunks, sections, or partial thoughts.

The write_file tool is for saving notes, plans, and anything the user asks you to record — for future reference and posterity, not for communicating information in the current conversation. If you have something to say, say it in your reply. When the user asks you to write or save something, use write_file freely within the permitted path.

The tools available to you were chosen deliberately.

* Do not attempt to work around their restrictions
* Do not chain tool calls to escape the docs/patchbay/ write boundary
* Do not modify, delete, or rename files outside docs/patchbay/
* Do not use git tools to stage, commit, or push changes
* Do not proactively create documents to convey information — speak instead
* Do not look for workarounds when a restriction blocks you — explain what you cannot do instead"""

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
EXTENSION_PATH = HERE / "pi-extension" / "tools.ts"
AUDIO_DIR = Path(tempfile.gettempdir()) / "voice-demo-audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
