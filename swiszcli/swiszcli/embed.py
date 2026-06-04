"""Embedding client for swiszCLI P0.

Single function: embed(text) -> list[float]. Reuses the existing
nomic-embed-text model on the local Ollama endpoint. CPU-pinned per
swizmem config (keep_alive=24h, num_gpu=0) so chat models cant evict.

Fails LOUDLY. No silent fallbacks.
"""
from __future__ import annotations

import httpx

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "nomic-embed-text"


class EmbedError(RuntimeError):
    pass


class EmbedClient:
    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise EmbedError("embed() called with empty text")
        try:
            r = self._client.post(
                f"{self.base_url}/api/embeddings",
                json={
                    "model": self.model,
                    "prompt": text,
                    "options": {"num_gpu": 0},
                    "keep_alive": "24h",
                },
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            raise EmbedError(f"embed http failure: {exc}") from exc
        vec = data.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise EmbedError(f"embed returned no vector: {data!r}")
        return vec

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


_SINGLETON = None


def get_client() -> EmbedClient:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = EmbedClient()
    return _SINGLETON


def embed(text: str):
    return get_client().embed(text)
