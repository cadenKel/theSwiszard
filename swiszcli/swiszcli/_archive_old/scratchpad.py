"""Scratchpad reasoning: external structured memory for the 9b.

The model doesnt need bigger working memory if it has external memory it
can iterate on. Every multi-step task gets a scratchpad with goal, plan,
cursor, observations, blockers, decisions.
"""
from __future__ import annotations
import json
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".swiszcli" / "scratchpad.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scratchpads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    goal        TEXT    NOT NULL,
    plan_json   TEXT    NOT NULL,
    cursor      INTEGER NOT NULL DEFAULT 0,
    observations_json TEXT NOT NULL DEFAULT '[]',
    blockers_json     TEXT NOT NULL DEFAULT '[]',
    decisions_json    TEXT NOT NULL DEFAULT '[]',
    status      TEXT    NOT NULL DEFAULT 'active',
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    archived_at REAL
);
CREATE INDEX IF NOT EXISTS idx_scratch_session ON scratchpads(session_id);
CREATE INDEX IF NOT EXISTS idx_scratch_status  ON scratchpads(status);
"""


@dataclass
class Step:
    text: str
    done: bool = False
    result: str = ""


@dataclass
class Observation:
    step_idx: int
    action: str
    result: str
    ts: float = field(default_factory=time.time)


class Scratchpad:
    def __init__(self, conn, row_id, session_id, goal, plan,
                 cursor=0, observations=None, blockers=None,
                 decisions=None, status="active"):
        self._conn = conn
        self.id = row_id
        self.session_id = session_id
        self.goal = goal
        self.plan = plan
        self.cursor = cursor
        self.observations = observations or []
        self.blockers = blockers or []
        self.decisions = decisions or []
        self.status = status

    @property
    def is_done(self):
        return self.cursor >= len(self.plan) or self.status != "active"

    @property
    def current_step(self):
        if self.is_done:
            return None
        return self.plan[self.cursor]

    def observe(self, action, result):
        self.observations.append(
            Observation(step_idx=self.cursor, action=action, result=result)
        )
        self._persist()

    def complete_step(self, result_summary=""):
        if self.is_done:
            return
        self.plan[self.cursor].done = True
        self.plan[self.cursor].result = result_summary
        self.cursor += 1
        if self.cursor >= len(self.plan):
            self.status = "complete"
        self._persist()

    def add_blocker(self, text):
        self.blockers.append(text)
        self._persist()

    def add_decision(self, choice, why):
        self.decisions.append({
            "step": self.cursor, "choice": choice,
            "why": why, "ts": time.time(),
        })
        self._persist()

    def insert_step(self, text, at_index=None):
        idx = at_index if at_index is not None else self.cursor + 1
        self.plan.insert(idx, Step(text=text))
        self._persist()

    def abandon(self, reason=""):
        self.status = "abandoned"
        if reason:
            self.blockers.append("ABANDONED: " + reason)
        self._persist()

    def render(self, max_obs=10):
        nl = chr(10)
        out = ["<scratchpad>"]
        out.append("  goal: " + self.goal)
        out.append("  plan:")
        for i, step in enumerate(self.plan):
            if step.done:
                mark = "[x]"
            elif i == self.cursor:
                mark = "[>]"
            else:
                mark = "[ ]"
            line = "    " + mark + " " + str(i + 1) + ". " + step.text
            if step.done and step.result:
                line += " -> " + step.result[:80]
            out.append(line)
        if self.observations:
            out.append("  recent observations:")
            for o in self.observations[-max_obs:]:
                out.append("    step" + str(o.step_idx + 1) + ": "
                           + o.action[:80] + " -> " + o.result[:120])
        if self.decisions:
            out.append("  decisions:")
            for d in self.decisions[-5:]:
                out.append("    " + d["choice"] + " (because: "
                           + d["why"][:80] + ")")
        if self.blockers:
            out.append("  blockers:")
            for b in self.blockers[-3:]:
                out.append("    " + b[:120])
        if self.is_done:
            out.append("  status: " + self.status.upper())
        out.append("</scratchpad>")
        return nl.join(out)

    def _persist(self):
        plan_json = json.dumps([asdict(s) for s in self.plan])
        obs_json = json.dumps([asdict(o) for o in self.observations])
        blockers_json = json.dumps(self.blockers)
        decisions_json = json.dumps(self.decisions)
        self._conn.execute(
            "UPDATE scratchpads SET plan_json=?, cursor=?, "
            "observations_json=?, blockers_json=?, decisions_json=?, "
            "status=?, updated_at=? WHERE id=?",
            (plan_json, self.cursor, obs_json, blockers_json,
             decisions_json, self.status, time.time(), self.id),
        )
        if self.status != "active":
            r = self._conn.execute(
                "SELECT archived_at FROM scratchpads WHERE id=?",
                (self.id,)).fetchone()
            if not r["archived_at"]:
                self._conn.execute(
                    "UPDATE scratchpads SET archived_at=? WHERE id=?",
                    (time.time(), self.id))
        self._conn.commit()


class ScratchpadStore:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        self._conn.close()

    def create(self, session_id, goal, plan_steps):
        plan = [Step(text=s) for s in plan_steps]
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO scratchpads(session_id, goal, plan_json, "
            "cursor, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
            (session_id, goal,
             json.dumps([asdict(s) for s in plan]), now, now),
        )
        self._conn.commit()
        return Scratchpad(self._conn, cur.lastrowid,
                          session_id, goal, plan)

    def get_active(self, session_id):
        r = self._conn.execute(
            "SELECT * FROM scratchpads WHERE session_id=? AND status=? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, "active"),
        ).fetchone()
        if not r:
            return None
        return self._hydrate(r)

    def get_by_id(self, scratchpad_id):
        r = self._conn.execute(
            "SELECT * FROM scratchpads WHERE id=?",
            (scratchpad_id,),
        ).fetchone()
        if not r:
            return None
        return self._hydrate(r)

    def recent_archived(self, session_id=None, limit=5):
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM scratchpads WHERE status IN ('complete','abandoned') "
                "AND session_id=? ORDER BY archived_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM scratchpads WHERE status IN ('complete','abandoned') "
                "ORDER BY archived_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._hydrate(r) for r in rows]

    def _hydrate(self, r):
        plan_raw = json.loads(r["plan_json"])
        plan = [Step(**s) for s in plan_raw]
        obs_raw = json.loads(r["observations_json"])
        observations = [Observation(**o) for o in obs_raw]
        blockers = json.loads(r["blockers_json"])
        decisions = json.loads(r["decisions_json"])
        return Scratchpad(
            self._conn, r["id"], r["session_id"], r["goal"], plan,
            cursor=r["cursor"], observations=observations,
            blockers=blockers, decisions=decisions, status=r["status"],
        )
