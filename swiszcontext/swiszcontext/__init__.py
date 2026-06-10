"""
swiszcontext — conversation frame trace.

Stores overlapping conversation frames across sessions.
Retrieved by pure similarity (no triggers, no model calls).
Presented chronologically as a transcript collage.

Separate from swiszPM. Separate from swiszcli.
Background trace module — not called by the model directly.
"""
from .store import ContextStore

__all__ = ["ContextStore"]
