"""Runtime configuration, read once from environment variables.

No secret values are ever logged or returned.
"""
from __future__ import annotations

import os


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        # "anthropic" (default) or "openai" (any OpenAI-compatible Chat Completions API:
        # OpenAI, Groq, OpenRouter, Together, Gemini's OpenAI endpoint, Ollama, vLLM ...)
        self.llm_provider: str = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
        self.llm_model: str = os.getenv(
            "LLM_MODEL",
            "claude-haiku-4-5-20251001" if self.llm_provider == "anthropic" else "gpt-4o-mini",
        )
        self.anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
        self.anthropic_base_url: str = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self.openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
        self.openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

        self.llm_timeout_s: float = _float("LLM_TIMEOUT_SECONDS", 8.0)
        self.llm_max_attempts: int = int(_float("LLM_MAX_ATTEMPTS", 2))
        # Backup interpreter used ONLY if the LLM fails / returns unusable output.
        self.enable_rule_fallback: bool = _bool("ENABLE_RULE_FALLBACK", True)
        self.cache_size: int = int(_float("INTERPRETATION_CACHE_SIZE", 512))
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        return bool(self.openai_api_key) or "localhost" in self.openai_base_url or "127.0.0.1" in self.openai_base_url


settings = Settings()
