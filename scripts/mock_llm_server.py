"""Offline MOCK LLM server for demos and load tests without an API key.

Speaks both the Anthropic Messages API (forced tool_use) and the OpenAI Chat
Completions API. It answers using the rule-based backup parser, optionally with
artificial latency and random failures, so the full HTTP LLM path of the service
(request building, parsing, retries, guardrails, caching) can be exercised.

NOT for submission: the real service must use a real LLM.

    MOCK_LLM_DELAY=1.5 MOCK_LLM_FAIL_RATE=0.1 uvicorn scripts.mock_llm_server:app --port 9000
    ANTHROPIC_API_KEY=dummy ANTHROPIC_BASE_URL=http://127.0.0.1:9000 uvicorn app.main:app --port 8000
"""
import asyncio
import json
import os
import pathlib
import random
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.fallback_parser import interpret_notes  # noqa: E402

DELAY = float(os.getenv("MOCK_LLM_DELAY", "0"))
FAIL_RATE = float(os.getenv("MOCK_LLM_FAIL_RATE", "0"))
app = FastAPI()


def _notes(text: str) -> list[str]:
    part = text.split("Operator notes to interpret")[-1]
    return re.findall(r"^\[\d+\] (.*)$", part, re.M)


async def _answer(text: str):
    await asyncio.sleep(DELAY * random.uniform(0.6, 1.4))
    if random.random() < FAIL_RATE:
        return None
    return {"interpretations": [{**x, "explanation": "mock LLM"} for x in interpret_notes(_notes(text))]}


@app.post("/v1/messages")
async def anthropic(req: Request):
    body = await req.json()
    out = await _answer(body["messages"][0]["content"])
    if out is None:
        return JSONResponse(status_code=529, content={"type": "error"})
    return {"content": [{"type": "tool_use", "id": "mock", "name": body["tools"][0]["name"], "input": out}]}


@app.post("/v1/chat/completions")
async def openai(req: Request):
    body = await req.json()
    out = await _answer(body["messages"][-1]["content"])
    if out is None:
        return JSONResponse(status_code=503, content={"error": "mock failure"})
    return {"choices": [{"message": {"content": json.dumps(out)}}]}
