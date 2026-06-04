"""SQLite-persisted Wizard registry.

LLM- (or user-) authored wizards become JSON-serializable dicts and live
in <state_dir>/wizards.db. At boot, load_into_registry() rehydrates them
into the in-memory REGISTRY alongside the code-defined seed wizards
(mem.*, etc).

Security boundary: serialized wizards can only reference CALLABLES from
swiszcli.callables (whitelist). Anything else fails loud at load time.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import callables as wcall
from .wizard import REGISTRY, Step, Wizard, register


SCHEMA = """
CREATE TABLE IF NOT EXISTS wizards (
    name        TEXT PRIMARY KEY,
    def_json    TEXT NOT NULL,
    source      TEXT NOT NULL,   -- code | user | llm
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


# ── (de)serialization ───────────────────────────────────────────────────
def step_to_dict(s: Step) -> dict:
    d = {"key": s.key, "kind": s.kind, "prompt": s.prompt}
    # only emit fields that are non-default + serializable
    for fld in ("default", "multiline", "placeholder", "pool", "top_n",
                "new_prompt", "nested_wizard"):
        v = getattr(s, fld)
        if v not in (None, "", False, 10, "type a new option"):
            d[fld] = v
    # callables by name
    if s.choices is not None:
        d["choices_ref"] = _find_name(wcall.CHOICES, s.choices,    "choices")
    if s.validate is not None:
        d["validate_ref"] = _find_name(wcall.VALIDATORS, s.validate, "validate")
    if s.next is not None:
        d["next_ref"] = _find_name(wcall.NEXT, s.next, "next")
    if s.action is not None:
        d["action_ref"] = _find_name(wcall.ACTIONS, s.action, "action")
    return d


def _find_name(table: dict, fn, kind: str) -> str:
    for n, f in table.items():
        if f is fn:
            return n
    raise ValueError(f"callable for kind={kind} not in whitelist; "
                     f"register it in swiszcli.callables before saving")


def step_from_dict(d: dict) -> Step:
    kw = {k: d.get(k) for k in
          ("key", "kind", "prompt", "default", "multiline", "placeholder",
           "pool", "top_n", "new_prompt", "nested_wizard")
          if k in d}
    if "choices_ref" in d:
        kw["choices"] = _resolve(wcall.CHOICES, d["choices_ref"], "choices")
    if "validate_ref" in d:
        kw["validate"] = _resolve(wcall.VALIDATORS, d["validate_ref"], "validate")
    if "next_ref" in d:
        kw["next"] = _resolve(wcall.NEXT, d["next_ref"], "next")
    if "action_ref" in d:
        kw["action"] = _resolve(wcall.ACTIONS, d["action_ref"], "action")
    return Step(**kw)


def _resolve(table: dict, name: str, kind: str):
    if name not in table:
        raise KeyError(f"unknown {kind}_ref {name!r}; not in whitelist")
    return table[name]


def wizard_to_dict(w: Wizard) -> dict:
    if w.summary is not None or w.commit is not None:
        # commit/summary are code-only for now; data wizards have neither
        raise ValueError(f"wizard {w.name} has code commit/summary; "
                         f"cannot serialize. promote to a callable first")
    return {
        "name": w.name,
        "title": w.title,
        "steps": [step_to_dict(s) for s in w.steps],
    }


def wizard_from_dict(d: dict) -> Wizard:
    return Wizard(
        name=d["name"],
        title=d["title"],
        steps=[step_from_dict(s) for s in d["steps"]],
    )


# ── store ───────────────────────────────────────────────────────────────
class WizardStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def save(self, w: Wizard, source: str = "llm") -> None:
        if source not in ("code", "user", "llm"):
            raise ValueError(f"bad source {source!r}")
        body = json.dumps(wizard_to_dict(w), sort_keys=True)
        now = time.time()
        self._conn.execute(
            """INSERT INTO wizards (name, def_json, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                    def_json=excluded.def_json,
                    source=excluded.source,
                    updated_at=excluded.updated_at""",
            (w.name, body, source, now, now),
        )
        self._conn.commit()
        # register/replace in memory too
        REGISTRY[w.name] = w

    def delete(self, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM wizards WHERE name = ?", (name,))
        self._conn.commit()
        REGISTRY.pop(name, None)
        return cur.rowcount > 0

    def all(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT name, def_json, source, created_at, updated_at FROM wizards"
        ).fetchall()
        return [{"name": r[0], "def": json.loads(r[1]), "source": r[2],
                 "created_at": r[3], "updated_at": r[4]} for r in rows]

    def load_into_registry(self) -> int:
        n = 0
        for row in self.all():
            w = wizard_from_dict(row["def"])
            # do not collide with code-defined wizards already in REGISTRY
            if w.name in REGISTRY and REGISTRY[w.name] is not None:
                # code wins for now; data wizards overlay only if not present
                continue
            REGISTRY[w.name] = w
            n += 1
        return n


# process singleton
_DEFAULT: WizardStore | None = None


def set_default(store: WizardStore) -> None:
    global _DEFAULT
    _DEFAULT = store


def get_default() -> WizardStore | None:
    return _DEFAULT


# convenience for tests / wizard.author
def save(w: Wizard, source: str = "llm") -> None:
    s = get_default()
    if s is None:
        raise RuntimeError("WizardStore default not set")
    s.save(w, source=source)
