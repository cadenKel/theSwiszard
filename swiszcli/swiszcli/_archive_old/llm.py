"""Ollama chat client — streaming, model-agnostic."""
from __future__ import annotations

import json
from typing import Any, Iterator

import httpx


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.last_stats: dict = {}

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
        keep_alive: str | int = "24h",
    ) -> Iterator[str]:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive,
            "options": options or {},
        }
        with httpx.stream(
            "POST",
            f"{self.base_url}/api/chat",
            json=body,
            timeout=self.timeout,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("message") or {}
                chunk = msg.get("content", "")
                if chunk:
                    yield chunk
                if obj.get("done"):
                    self.last_stats = {
                        "eval_count": obj.get("eval_count", 0),
                        "eval_ms": obj.get("eval_duration", 0) // 1_000_000,
                        "prompt_eval_count": obj.get("prompt_eval_count", 0),
                        "prompt_eval_ms": obj.get("prompt_eval_duration", 0) // 1_000_000,
                        "load_ms": obj.get("load_duration", 0) // 1_000_000,
                        "total_ms": obj.get("total_duration", 0) // 1_000_000,
                    }
                    return
from .llm_cloud import CloudClient


def make_llm(cfg):
    """Create the right chat client based on cfg.provider.

    Returns an object with .chat_stream(messages) -> Iterator[str] and .last_stats dict.
    """
    if cfg.provider == "ollama":
        return OllamaClient(cfg.ollama_url, cfg.model)

    # Cloud provider (deepseek, openrouter)
    return CloudClient(
        base_url=cfg.provider_base_url,
        api_key=cfg.provider_api_key,
        model=cfg.model,
    )
