"""OpenAI-compatible chat client for cloud providers.

Same streaming interface as llm.OllamaClient so anything using
chat_stream(messages) -> Iterator[str] can swap between local/cloud
transparently. Works with: DeepSeek, OpenRouter.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

import httpx


class CloudClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
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
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if options:
            body.update(options)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        t0 = time.monotonic()
        eval_count = 0
        with httpx.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=body,
            headers=headers,
            timeout=self.timeout,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                s = line.strip()
                if not s.startswith("data: "):
                    continue
                payload = s[6:]
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                for choice in choices:
                    delta = choice.get("delta") or {}
                    content = delta.get("content", "")
                    if content:
                        eval_count += 1
                        yield content
                usage = obj.get("usage") or {}
                if usage:
                    self.last_stats = {
                        "eval_count": usage.get("completion_tokens", eval_count),
                        "prompt_eval_count": usage.get("prompt_tokens", 0),
                    }
                if obj.get("done"):
                    break

        dt = time.monotonic() - t0
        self.last_stats["eval_ms"] = self.last_stats.get("eval_ms", int(dt * 1000))
        self.last_stats["total_ms"] = self.last_stats.get("total_ms", int(dt * 1000))
