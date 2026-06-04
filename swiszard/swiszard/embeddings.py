"""
embeddings.py — CPU-only sentence-transformers wrapper with lazy singleton cache.

Uses sentence-transformers/all-MiniLM-L6-v2 (~80 MB) exclusively on CPU.
The model is loaded once per process and cached in module-level state.

IMPORTANT: device="cpu" is set explicitly — this must never use the GPU.
"""
from __future__ import annotations

import numpy as np
from typing import Optional

_model = None  # module-level singleton


def _get_model():
    """Lazily load and cache the embedding model (CPU-only)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer  # type: ignore
        _model = SentenceTransformer(
            "sentence-transformers/all-MiniLM-L6-v2", device="cpu"
        )
    return _model


def embed(text: str) -> np.ndarray:
    """Embed a text string and return a float32 numpy array."""
    model = _get_model()
    return model.encode(text, convert_to_numpy=True)


def embed_to_blob(text: str) -> bytes:
    """Embed text and serialise to raw bytes for SQLite BLOB storage."""
    arr = embed(text)
    return arr.astype(np.float32).tobytes()


def blob_to_array(blob: bytes) -> np.ndarray:
    """Deserialise a BLOB back to a float32 numpy array."""
    return np.frombuffer(blob, dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Return cosine similarity between two vectors (range -1 to 1)."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))
