"""WeightEngine: unified EMA weight store for all swiszcli weight domains.

All weighted subsystems (chain_credit edges, source_weights, sequence_learn)
delegate to this class via namespaced keys.

Key namespaces:
  edge:{from}:{to}   — chain credit transition weights
  source:{name}      — source trust multipliers
  seq:{sid}          — sequence step weights

EMA update:  new = alpha * observed + (1 - alpha) * old
Persisted to ~/.swiszcli/weight_engine.json (one JSON object).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_PATH = Path.home() / ".swiszcli" / "weight_engine.json"

@dataclass
class WeightEngine:
    db_path: Path = field(default_factory=lambda: _DEFAULT_PATH)
    alpha: float = 0.15        # EMA learning rate
    default_weight: float = 0.5
    _data: dict = field(default_factory=dict, repr=False)
    _dirty: bool = field(default=False, repr=False)

    def __post_init__(self):
        self.db_path = Path(self.db_path)
        self._load()

    # ---- persistence ---------------------------------------------------------

    def _load(self):
        if self.db_path.is_file():
            try:
                self._data = json.loads(self.db_path.read_text())
            except Exception:
                self._data = {}
        else:
            self._data = {}

    def save(self):
        if self._dirty:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path.write_text(json.dumps(self._data, indent=2))
            self._dirty = False

    # ---- core API ------------------------------------------------------------

    def get(self, key: str) -> float:
        """Return current weight for key, default_weight if unseen."""
        entry = self._data.get(key)
        if entry is None:
            return self.default_weight
        return entry["w"]

    def observe(self, key: str, value: float) -> float:
        """Apply EMA update with observed value. Returns new weight."""
        old = self.get(key)
        new = self.alpha * value + (1 - self.alpha) * old
        self._data[key] = {"w": round(new, 6), "n": self._data.get(key, {}).get("n", 0) + 1, "ts": time.time()}
        self._dirty = True
        return new

    def set(self, key: str, value: float):
        """Hard-set a weight (for seeding defaults)."""
        self._data[key] = {"w": float(value), "n": 0, "ts": time.time()}
        self._dirty = True

    def keys(self, prefix: str = "") -> list[str]:
        return [k for k in self._data if k.startswith(prefix)]

    def snapshot(self, prefix: str = "") -> dict[str, float]:
        """Return {key: weight} for all keys matching prefix."""
        return {k: v["w"] for k, v in self._data.items() if k.startswith(prefix)}

    # ---- namespaced helpers --------------------------------------------------

    def edge_weight(self, from_name: str, to_name: str) -> float:
        return self.get(f"edge:{from_name}:{to_name}")

    def observe_edge(self, from_name: str, to_name: str, value: float) -> float:
        return self.observe(f"edge:{from_name}:{to_name}", value)

    def source_weight(self, source: str) -> float:
        return self.get(f"source:{source}")

    def observe_source(self, source: str, value: float) -> float:
        return self.observe(f"source:{source}", value)

    def seq_weight(self, sid: int | str) -> float:
        return self.get(f"seq:{sid}")

    def observe_seq(self, sid: int | str, value: float) -> float:
        return self.observe(f"seq:{sid}", value)


# Module-level singleton (lazy init)
_engine: WeightEngine | None = None

def get_engine(db_path: Path | None = None) -> WeightEngine:
    global _engine
    if _engine is None:
        _engine = WeightEngine(db_path=db_path or _DEFAULT_PATH)
    return _engine
