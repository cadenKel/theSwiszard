"""P1.9 trajectory predictor: where is this conversation heading?

Keeps a rolling buffer of recent turn embeddings. Each turn is a point in
embedding space; the conversation is a trajectory. Compute drift vector
(EMA of step deltas), extrapolate one step ahead, and expose the
predicted point so the void detector can pre-fetch knowledge BEFORE the
model needs it.

Pure numpy-free vector math. No LLM.

Usage:
    traj = Trajectory(window=8)
    traj.add(embed(user_text))
    next_point = traj.predict_next()   # vector or None
    if next_point is not None: ...

Persistence:
    traj.save(path)   # write to JSON file
    Trajectory.load(path)  # restore from file (returns new instance)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _vec_add(a, b):    return [x + y for x, y in zip(a, b)]
def _vec_sub(a, b):    return [x - y for x, y in zip(a, b)]
def _vec_scale(a, s):  return [x * s for x in a]
def _vec_norm(a):      return sum(x * x for x in a) ** 0.5


@dataclass
class Trajectory:
    window: int = 8
    drift_alpha: float = 0.6  # EMA weight for newest step
    points: list = field(default_factory=list)
    _drift: Optional[list] = None

    def add(self, vec):
        if not vec:
            return
        if self.points:
            step = _vec_sub(vec, self.points[-1])
            if self._drift is None:
                self._drift = step
            else:
                self._drift = _vec_add(
                    _vec_scale(step, self.drift_alpha),
                    _vec_scale(self._drift, 1.0 - self.drift_alpha),
                )
        self.points.append(list(vec))
        if len(self.points) > self.window:
            self.points = self.points[-self.window :]

    def predict_next(self):
        """Linear extrapolation: last point + EMA drift. Needs ≥2 points."""
        if len(self.points) < 2 or self._drift is None:
            return None
        return _vec_add(self.points[-1], self._drift)

    def drift_magnitude(self):
        if self._drift is None:
            return 0.0
        return _vec_norm(self._drift)

    def is_settled(self, threshold=0.05):
        """True if drift is small — conversation is drilling, not exploring."""
        return self.drift_magnitude() < threshold

    def reset(self):
        self.points.clear()
        self._drift = None

    # ── cross-session persistence ────────────────────────────────────────────

    def save(self, path) -> None:
        """Persist points + drift to JSON. Atomic write via temp file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "window": self.window,
            "drift_alpha": self.drift_alpha,
            "points": self.points,
            "_drift": self._drift,
        }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)

    @classmethod
    def load(cls, path) -> "Trajectory":
        """Restore from JSON file. Returns a fresh Trajectory on any error."""
        try:
            data = json.loads(Path(path).read_text())
            t = cls(
                window=data.get("window", 8),
                drift_alpha=data.get("drift_alpha", 0.6),
                points=data.get("points", []),
            )
            t._drift = data.get("_drift")
            return t
        except Exception:
            return cls()
