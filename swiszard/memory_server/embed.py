"""
embed.py — Embeddings via nomic-embed-text through Ollama.

nomic-embed-text produces 768-dim float32 vectors.
Lives outside the main LLM VRAM budget — runs on CPU.
"""
from __future__ import annotations

import json
import struct
import urllib.request
from typing import TYPE_CHECKING

import numpy as np

OLLAMA_URL = "http://127.0.0.1:11434/api/embed"
EMBED_MODEL = "nomic-embed-text:latest"


def embed(text: str) -> np.ndarray:
    """Call Ollama nomic-embed-text and return a float32 numpy array."""
    payload = json.dumps({"model": EMBED_MODEL, "input": text, "options": {"num_gpu": 0}, "keep_alive": "24h"}).encode()
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    # Ollama returns { "embeddings": [[...]] }
    vec = result["embeddings"][0]
    return np.array(vec, dtype=np.float32)


def embed_to_blob(text: str) -> bytes:
    return embed(text).tobytes()


def blob_to_array(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def top_k_rows(query: str, rows: list, vec_field: str, k: int,
               recency_lambda: float = 0.15, recency_tau_days: float = 14.0) -> list[tuple[float, any]]:
    """Return top-k rows sorted by cosine + recency bias.

    score = cosine + lambda * exp(-age_days / tau)
    Time is not flat: fresher rows get a thumb on the scale.
    Set recency_lambda=0.0 to disable and fall back to pure cosine.
    """
    import math as _math, time as _time
    q_vec = embed(query)
    now = _time.time()
    tau_seconds = recency_tau_days * 86400.0
    scored = []
    for row in rows:
        r_vec = blob_to_array(bytes(row[vec_field]))
        sim = cosine_similarity(q_vec, r_vec)
        try:
            ts = float(row["timestamp"])
        except Exception:
            ts = now
        age = max(0.0, now - ts)
        bias = recency_lambda * _math.exp(-age / tau_seconds) if recency_lambda else 0.0
        scored.append((sim + bias, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:k]
