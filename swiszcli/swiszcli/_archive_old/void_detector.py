"""P1.7 void detector: density-based gap finding in embedding space.

Treats memory bodies + recently-loaded context chunks as point masses in
embedding space. Given a query/phrase, computes local mass density (sum of
Gaussian-kernel contributions from corpus points within radius). Low
density = semantic void = the corpus has nothing nearby and the model
will fabricate if it proceeds.

NO LLM in this module. Pure numpy + the existing nomic embedder.

Plays alongside gap_detector.py:
  - gap_detector reads the model's WORDS (hedge, fabrication, external)
  - void_detector reads the model's EMBEDDED INTENT vs corpus mass
Either trigger fires the research wizard.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class VoidVerdict:
    has_void: bool
    density: float
    threshold: float
    nearest_score: float = 0.0
    nearest_id: int | None = None
    query_seed: str = ""

    @property
    def summary(self):
        return f"density={self.density:.3f} thr={self.threshold:.3f} nearest={self.nearest_score:.2f}"


def _cosine(a, b):
    da = sum(x * x for x in a) ** 0.5
    db = sum(x * x for x in b) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (da * db)


def density(query_vec, corpus_vecs, *, bandwidth=0.25):
    """KDE-style density at query point using cosine-distance Gaussian kernel.

    bandwidth (h) is small because cosine similarities cluster tight; 0.25
    means a memory at sim=0.75 contributes ~0.37 mass."""
    if not corpus_vecs:
        return 0.0, 0.0, None
    h2 = 2 * bandwidth * bandwidth
    total = 0.0
    best_sim = -1.0
    best_idx = None
    for i, v in enumerate(corpus_vecs):
        s = _cosine(query_vec, v)
        d = 1.0 - s  # cosine distance
        total += math.exp(-(d * d) / h2)
        if s > best_sim:
            best_sim = s
            best_idx = i
    return total, best_sim, best_idx


def detect(phrase, *, embed_fn, corpus_provider, threshold=0.40):
    """Return VoidVerdict for a single phrase.

    corpus_provider() must return list[(memory_id_or_None, embedding_vec)].
    threshold: minimum density to consider 'not a void'. Tunable.
    """
    phrase = (phrase or "").strip()
    if not phrase:
        return VoidVerdict(has_void=False, density=0.0, threshold=threshold)
    try:
        qv = embed_fn(phrase)
    except Exception:
        return VoidVerdict(has_void=False, density=0.0, threshold=threshold, query_seed=phrase)
    corpus = corpus_provider() or []
    if not corpus:
        # No corpus = everything is a void, but firing on cold-start is noise.
        return VoidVerdict(has_void=False, density=0.0, threshold=threshold, query_seed=phrase)
    ids = [c[0] for c in corpus]
    vecs = [c[1] for c in corpus]
    dens, nearest_sim, nearest_idx = density(qv, vecs)
    return VoidVerdict(
        has_void=(dens < threshold),
        density=dens,
        threshold=threshold,
        nearest_score=nearest_sim,
        nearest_id=ids[nearest_idx] if nearest_idx is not None else None,
        query_seed=phrase,
    )


def extract_claim_phrases(text, *, max_phrases=3, min_words=4):
    """Cheap claim-phrase extractor: sentences that look like factual claims.

    Heuristic, no NLP libs: pick sentences with a verb-ish pattern, skip
    questions and short fragments. Returns up to max_phrases.
    """
    import re
    if not text:
        return []
    sents = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for s in sents:
        s = s.strip()
        if not s or s.endswith("?"):
            continue
        words = s.split()
        if len(words) < min_words:
            continue
        # Skip pure code fences / shell lines
        if s.startswith(("$", ">>>", "```", "#")):
            continue
        out.append(s[:200])
        if len(out) >= max_phrases:
            break
    return out
