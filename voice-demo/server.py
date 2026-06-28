#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
#     "python-multipart",
#     "faster-whisper",
# ]
# ///
"""
Voice-chat demo server — pi backend, multi-chat, Tailscale-hosted.

Run:
    cd voice-demo && uv run server.py

Expose via Tailscale (required for mic access on iOS):
    tailscale serve --bg http://localhost:8800
"""

from __future__ import annotations

from app import app  # noqa: F401 — uvicorn target
from config import HOST, PORT


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
