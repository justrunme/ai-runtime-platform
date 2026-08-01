"""Minimal OpenAI-compatible mock backend for platform e2e."""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="mock-openai")
_stats = {"chat_completions": 0, "stream_completions": 0}


def _stream_delay_seconds(payload: dict) -> float:
    """Delay only for explicit slow-stream probes (or MOCK_STREAM_DELAY_SECONDS)."""
    messages = payload.get("messages") or []
    for message in messages:
        if isinstance(message, dict) and "e2e-slow-stream" in str(message.get("content", "")):
            return 20.0
    raw = os.getenv("MOCK_STREAM_DELAY_SECONDS", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/stats")
async def stats() -> dict[str, int]:
    return dict(_stats)


@app.post("/stats/reset")
async def reset_stats() -> dict[str, str]:
    _stats["chat_completions"] = 0
    _stats["stream_completions"] = 0
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    payload = await request.json()
    _stats["chat_completions"] += 1
    if payload.get("stream"):
        _stats["stream_completions"] += 1
        if request.headers.get("x-mock-stream-fail") == "1":

            async def broken():
                yield b'data: {"id":"chatcmpl-mock","choices":[{"delta":{"content":"hi"}}]}\n\n'
                raise RuntimeError("upstream interrupted")

            return StreamingResponse(broken(), media_type="text/event-stream")
        delay = _stream_delay_seconds(payload)
        if request.headers.get("x-mock-stream-slow") == "1" or delay > 0:

            async def slow():
                yield b'data: {"id":"chatcmpl-mock","choices":[{"delta":{"content":"hi"}}]}\n\n'
                await asyncio.sleep(delay if delay > 0 else 30)
                yield (
                    b'data: {"id":"chatcmpl-mock","choices":[{"delta":{"content":" bye"}}]}\n\n'
                    b"data: [DONE]\n\n"
                )

            return StreamingResponse(slow(), media_type="text/event-stream")
        body = (
            b'data: {"id":"chatcmpl-mock","choices":[{"delta":{"content":"hi"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return StreamingResponse(iter([body]), media_type="text/event-stream")
    return JSONResponse(
        {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello from mock"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
