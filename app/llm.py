"""Minimal async LLM clients (no vendor SDK needed; plain httpx).

Both return the parsed JSON object produced by the model, or raise LLMError.
Error messages never include API keys or raw provider responses.

On HTTP 429 we surface the Retry-After / x-ratelimit-reset-* hint by raising
LLMError with a `retry_after_s` attribute so the caller can back off exactly.
"""
from __future__ import annotations

import json
import re

import httpx

from .config import settings
from .prompt import OUTPUT_SCHEMA, SYSTEM_PROMPT, TOOL_NAME


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool = True, retry_after_s: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_s = retry_after_s


NON_RETRYABLE = {400, 401, 403, 404, 422}


def _status_error(code: int, response: httpx.Response | None = None) -> LLMError:
    retry_after = None
    if response is not None:
        # Try the standard header first, then Groq/OpenAI-specific rate-limit hints.
        ra = response.headers.get("retry-after")
        if ra:
            try:
                retry_after = float(ra)
            except ValueError:
                # Could be an HTTP-date; leave None and let caller use a default.
                pass
        if retry_after is None:
            for h in ("x-ratelimit-reset-ms", "x-ratelimit-reset"):
                v = response.headers.get(h)
                if v:
                    try:
                        reset_in = float(v) / (1000.0 if h.endswith("-ms") else 1.0)
                        # Groq returns "ms-until-reset" from "now"; clamp to [0, 60].
                        retry_after = max(0.0, min(reset_in, 60.0))
                    except ValueError:
                        pass
                    break
    return LLMError(f"provider returned HTTP {code}",
                    retryable=code not in NON_RETRYABLE,
                    retry_after_s=retry_after)


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.llm_timeout_s)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _anthropic(user_msg: str) -> dict:
    if not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set", retryable=False)
    payload = {
        "model": settings.llm_model,
        "max_tokens": 1500,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "tools": [{
            "name": TOOL_NAME,
            "description": "Record one structured interpretation per operator note.",
            "input_schema": OUTPUT_SCHEMA,
        }],
        # Forced tool call = Claude's documented structured-output path.
        "tool_choice": {"type": "tool", "name": TOOL_NAME},
        "messages": [{"role": "user", "content": user_msg}],
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        r = await client().post(f"{settings.anthropic_base_url.rstrip('/')}/v1/messages",
                                json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMError(f"provider request failed ({type(exc).__name__})") from None
    if r.status_code != 200:
        raise _status_error(r.status_code, r)
    data = r.json()
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
            return block.get("input") or {}
    raise LLMError("provider response contained no structured tool output")


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise LLMError("model output was not valid JSON")


async def _openai_compatible(user_msg: str) -> dict:
    system = (SYSTEM_PROMPT + "\n\nRespond with ONLY a JSON object matching this JSON Schema "
              "(no markdown, no prose):\n" + json.dumps(OUTPUT_SCHEMA))
    payload = {
        "model": settings.llm_model,
        "temperature": 0,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    }
    headers = {"content-type": "application/json"}
    if settings.openai_api_key:
        headers["authorization"] = f"Bearer {settings.openai_api_key}"
    try:
        r = await client().post(f"{settings.openai_base_url.rstrip('/')}/chat/completions",
                                json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMError(f"provider request failed ({type(exc).__name__})") from None
    if r.status_code != 200:
        raise _status_error(r.status_code, r)
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise LLMError("unexpected provider response shape") from None
    return _extract_json(content or "")


async def call_llm(user_msg: str) -> dict:
    if settings.llm_provider == "anthropic":
        return await _anthropic(user_msg)
    if settings.llm_provider in ("openai", "openai_compatible"):
        return await _openai_compatible(user_msg)
    raise LLMError(f"unknown LLM_PROVIDER {settings.llm_provider!r}", retryable=False)
