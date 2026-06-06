"""
projects.py — Project-manager substrate for swiszmem.

Shares the swiszmem sqlite DB and nomic-embed-text pipeline.

Tables:
  pm_project   — named project root
  pm_node      — tree node {objective|task|decision|question|artifact|note}, parent_id -> pm_node(id)
  pm_trigger   — situational triggers per node (proactive injection)
  pm_frame     — overlapping sentence-windows per node (retrieval units)
  pm_conflict  — async queue: similar/contradictory node pairs to review
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from typing import Any

from memory_server.embed import embed_to_blob, embed, blob_to_array, cosine_similarity

NODE_KINDS = {"objective", "task", "decision", "question", "artifact", "note", "north_star"}
NODE_STATES = {"proposed", "active", "blocked", "done", "abandoned", "deprecated", "committed", "superseded", "reverted", "open", "researching", "answered", "invalidated", "parked", "removed", "archived", "satisfied"}

FRAME_WINDOW_SENTENCES = 3
FRAME_STRIDE = 1
CONFLICT_SIM_THRESHOLD = 0.88


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pm_project (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    meta TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS pm_node (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES pm_project(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES pm_node(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'objective',
    state TEXT NOT NULL DEFAULT 'proposed',
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    body_vec BLOB NOT NULL,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    tags TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS pm_trigger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES pm_node(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_frame (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES pm_node(id) ON DELETE CASCADE,
    start_off INTEGER NOT NULL,
    end_off INTEGER NOT NULL,
    text TEXT NOT NULL,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_conflict (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_a INTEGER NOT NULL REFERENCES pm_node(id) ON DELETE CASCADE,
    node_b INTEGER NOT NULL REFERENCES pm_node(id) ON DELETE CASCADE,
    similarity REAL NOT NULL,
    detected INTEGER NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_pm_node_project ON pm_node(project_id);
CREATE INDEX IF NOT EXISTS idx_pm_node_parent ON pm_node(parent_id);
CREATE INDEX IF NOT EXISTS idx_pm_frame_node ON pm_frame(node_id);
CREATE INDEX IF NOT EXISTS idx_pm_trigger_node ON pm_trigger(node_id);
CREATE INDEX IF NOT EXISTS idx_pm_conflict_open ON pm_conflict(resolved);
"""


# ── helpers ──────────────────────────────────────────────────────────────────

def _derive_title(body: str, max_len: int = 80) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#-*> ").strip()
        if line:
            return line[:max_len]
    return body.strip()[:max_len] or "(empty)"


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences_with_offsets(text: str) -> list[tuple[int, int, str]]:
    out = []
    pos = 0
    for m in _SENT_SPLIT.finditer(text):
        end = m.start()
        if end > pos:
            seg = text[pos:end].strip()
            if seg:
                out.append((pos, end, seg))
        pos = m.end()
    if pos < len(text):
        seg = text[pos:].strip()
        if seg:
            out.append((pos, len(text), seg))
    return out


def build_frames(text: str, window: int = FRAME_WINDOW_SENTENCES,
                 stride: int = FRAME_STRIDE) -> list[tuple[int, int, str]]:
    sents = _sentences_with_offsets(text)
    if not sents:
        return []
    if len(sents) <= window:
        return [(sents[0][0], sents[-1][1], " ".join(s for _, _, s in sents))]
    out = []
    i = 0
    while i < len(sents):
        chunk = sents[i:i + window]
        if not chunk:
            break
        start = chunk[0][0]
        end = chunk[-1][1]
        out.append((start, end, " ".join(s for _, _, s in chunk)))
        if i + window >= len(sents):
            break
        i += stride
    return out


# ── project name validation ─────────────────────────────────────────────

def _sanitize_project_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("project name cannot be empty")
    # reject control chars (includes \n, \r, \t)
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("project name contains control characters")
    return name

# ── project CRUD ─────────────────────────────────────────────────────────────

def get_or_create_project(conn: sqlite3.Connection, name: str) -> int:
    name = _sanitize_project_name(name)
    row = conn.execute("SELECT id FROM pm_project WHERE name=?", (name,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO pm_project (name, created) VALUES (?, ?)",
                       (name, int(time.time())))
    conn.commit()
    return cur.lastrowid


def get_project_by_name(conn, name: str):
    name = _sanitize_project_name(name)
    return conn.execute("SELECT * FROM pm_project WHERE name=?", (name,)).fetchone()


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.id, p.name, p.created, "
        "(SELECT COUNT(*) FROM pm_node n WHERE n.project_id=p.id) AS node_count "
        "FROM pm_project p ORDER BY p.created DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ── node CRUD ────────────────────────────────────────────────────────────────

def insert_node(conn, project_id: int, body: str, kind: str = "objective",
                state: str = "proposed", parent_id=None, tags=None,
                title: str | None = None) -> int:
    if kind not in NODE_KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if state not in NODE_STATES:
        raise ValueError(f"unknown state {state!r}")
    if kind == 'north_star':
        existing = conn.execute(
            "SELECT id FROM pm_node WHERE project_id=? AND kind='north_star'",
            (project_id,)
        ).fetchone()
        if existing:
            raise ValueError(
                f"project already has a north_star node (#{existing[0]}). "
                f"Only one north_star is allowed per project."
            )
    now = int(time.time())
    title = title or _derive_title(body)
    body_vec = embed_to_blob(body)
    cur = conn.execute(
        "INSERT INTO pm_node (project_id, parent_id, kind, state, title, body, "
        "body_vec, created, updated, tags) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project_id, parent_id, kind, state, title, body, body_vec,
         now, now, json.dumps(tags or [])),
    )
    conn.commit()
    node_id = cur.lastrowid
    for s_off, e_off, frame_text in build_frames(body):
        fv = embed_to_blob(frame_text)
        conn.execute(
            "INSERT INTO pm_frame (node_id, start_off, end_off, text, vec) "
            "VALUES (?,?,?,?,?)",
            (node_id, s_off, e_off, frame_text, fv),
        )
    conn.commit()
    return node_id


def insert_trigger(conn, node_id: int, text: str) -> int:
    vec = embed_to_blob(text)
    cur = conn.execute(
        "INSERT INTO pm_trigger (node_id, text, vec) VALUES (?,?,?)",
        (node_id, text, vec),
    )
    conn.commit()
    return cur.lastrowid


def get_node(conn, node_id: int):
    return conn.execute("SELECT * FROM pm_node WHERE id=?", (node_id,)).fetchone()


def project_tree(conn, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, parent_id, kind, state, title, created, updated, "
        "COALESCE(tags, '[]') as tags "
        "FROM pm_node WHERE project_id=? ORDER BY created",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def propose_parent(conn, project_id: int, body: str, top_k: int = 5) -> list[dict]:
    """Suggest parent nodes by similarity to existing tree."""
    rows = conn.execute(
        "SELECT id, title, body_vec FROM pm_node WHERE project_id=?",
        (project_id,),
    ).fetchall()
    if not rows:
        return []
    qv = blob_to_array(embed_to_blob(body))
    scored = []
    for r in rows:
        sim = cosine_similarity(qv, blob_to_array(bytes(r["body_vec"])))
        scored.append({"id": r["id"], "title": r["title"], "score": sim})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]




# ── migration (legacy kinds/states) ─────────────────────────────────────────

def migrate_legacy_nodes(conn) -> dict:
    """Idempotent migration for legacy kinds/states."""
    updates = {}
    cur = conn.execute("UPDATE pm_node SET kind='objective' WHERE kind IN ('idea','goal')")
    updates['kind_to_objective'] = cur.rowcount
    cur = conn.execute("UPDATE pm_node SET state='proposed' WHERE state='idea'")
    updates['state_idea_to_proposed'] = cur.rowcount
    cur = conn.execute("UPDATE pm_node SET state='done' WHERE state='shipped'")
    updates['state_shipped_to_done'] = cur.rowcount
    cur = conn.execute("UPDATE pm_node SET state='abandoned' WHERE state='dead'")
    updates['state_dead_to_abandoned'] = cur.rowcount
    # adjustments
    cur = conn.execute("UPDATE pm_node SET state='archived' WHERE kind='note' AND state='deprecated'")
    updates['note_deprecated_to_archived'] = cur.rowcount
    cur = conn.execute("UPDATE pm_node SET state='active' WHERE kind='objective' AND state='blocked'")
    updates['objective_blocked_to_active'] = cur.rowcount
    cur = conn.execute("UPDATE pm_node SET state='satisfied' WHERE kind='objective' AND state='done'")
    updates['objective_done_to_satisfied'] = cur.rowcount
    conn.commit()
    return updates
# ── schema defaults migration ──────────────────────────────────────────

def migrate_schema_defaults(conn) -> dict:
    """Fix legacy pm_node defaults (idea/idea) to objective/proposed."""
    updates = {"rebuild_pm_node": 0}
    # detect current defaults
    rows = conn.execute("PRAGMA table_info(pm_node)").fetchall()
    dflts = {r[1]: r[4] for r in rows}
    kind_def = (dflts.get("kind") or "").strip("'\"")
    state_def = (dflts.get("state") or "").strip("'\"")
    if kind_def == "objective" and state_def == "proposed":
        return updates

    # rebuild table with correct defaults
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE pm_node RENAME TO pm_node_old")
    conn.execute(
        "CREATE TABLE pm_node ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "project_id INTEGER NOT NULL REFERENCES pm_project(id) ON DELETE CASCADE,"
        "parent_id INTEGER REFERENCES pm_node(id) ON DELETE CASCADE,"
        "kind TEXT NOT NULL DEFAULT 'objective',"
        "state TEXT NOT NULL DEFAULT 'proposed',"
        "title TEXT NOT NULL,"
        "body TEXT NOT NULL,"
        "body_vec BLOB NOT NULL,"
        "created INTEGER NOT NULL,"
        "updated INTEGER NOT NULL,"
        "tags TEXT DEFAULT '[]'"
        ")"
    )
    conn.execute(
        "INSERT INTO pm_node (id, project_id, parent_id, kind, state, title, body, body_vec, created, updated, tags) "
        "SELECT id, project_id, parent_id, kind, state, title, body, body_vec, created, updated, tags FROM pm_node_old"
    )
    conn.execute("DROP TABLE pm_node_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pm_node_project ON pm_node(project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pm_node_parent ON pm_node(parent_id)")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    updates["rebuild_pm_node"] = 1
    return updates


# ── project hygiene ─────────────────────────────────────────────────────

def prune_bad_projects(conn) -> dict:
    """Delete empty projects with invalid names (control chars or blank)."""
    cur = conn.execute(
        "DELETE FROM pm_project WHERE id IN ("
        "  SELECT p.id FROM pm_project p "
        "  LEFT JOIN pm_node n ON n.project_id=p.id "
        "  GROUP BY p.id HAVING COUNT(n.id)=0"
        ") AND (instr(name, char(10))>0 OR instr(name, char(13))>0 OR trim(name)='')"
    )
    conn.commit()
    return {"deleted": cur.rowcount}


# ── conflict scan ────────────────────────────────────────────────────────────

def scan_conflicts(conn, node_id: int,
                   threshold: float = CONFLICT_SIM_THRESHOLD) -> list[int]:
    row = get_node(conn, node_id)
    if not row:
        return []
    project_id = row["project_id"]
    qv = blob_to_array(bytes(row["body_vec"]))
    others = conn.execute(
        "SELECT id, body_vec FROM pm_node WHERE project_id=? AND id<>?",
        (project_id, node_id),
    ).fetchall()
    queued = []
    now = int(time.time())
    for o in others:
        sim = cosine_similarity(qv, blob_to_array(bytes(o["body_vec"])))
        if sim >= threshold:
            cur = conn.execute(
                "INSERT INTO pm_conflict (node_a, node_b, similarity, detected) "
                "VALUES (?,?,?,?)",
                (node_id, o["id"], float(sim), now),
            )
            queued.append(cur.lastrowid)
    conn.commit()
    return queued


def open_conflicts(conn, project_id: int | None = None) -> list[dict]:
    if project_id is None:
        rows = conn.execute(
            "SELECT c.*, na.title AS title_a, nb.title AS title_b "
            "FROM pm_conflict c "
            "JOIN pm_node na ON na.id=c.node_a "
            "JOIN pm_node nb ON nb.id=c.node_b "
            "WHERE c.resolved=0 ORDER BY c.detected DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT c.*, na.title AS title_a, nb.title AS title_b "
            "FROM pm_conflict c "
            "JOIN pm_node na ON na.id=c.node_a "
            "JOIN pm_node nb ON nb.id=c.node_b "
            "WHERE c.resolved=0 AND na.project_id=? "
            "ORDER BY c.detected DESC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_conflict(conn, conflict_id: int, resolution: str) -> bool:
    cur = conn.execute(
        "UPDATE pm_conflict SET resolved=1, resolution=? WHERE id=?",
        (resolution, conflict_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ── frame retrieval (overlap-aware) ──────────────────────────────────────────

def _intersects(a_node, a_start, a_end, b_node, b_start, b_end) -> bool:
    if a_node != b_node:
        return False
    return not (a_end <= b_start or b_end <= a_start)


def inject_frames(conn, query: str, top_k: int = 4,
                  active_project_id: int | None = None,
                  recency_lambda: float = 0.15,
                  recency_tau_days: float = 30.0,
                  active_boost: float = 0.1) -> list[dict]:
    """Return ranked frames with non-overlap invariant within same node.

    Score = max(sim_to_query, max_sim_to_trigger_of_node)
          + recency_lambda * exp(-age_days/tau)
          + (active_boost if frame's project == active_project_id else 0)
    """
    qv = blob_to_array(embed_to_blob(query))

    # Build per-node max trigger similarity
    trig_rows = conn.execute(
        "SELECT t.node_id, t.vec FROM pm_trigger t "
        "JOIN pm_node n ON n.id=t.node_id WHERE n.state NOT IN ('dead', 'deprecated')"
    ).fetchall()
    node_trigger_max = {}
    for tr in trig_rows:
        s = cosine_similarity(qv, blob_to_array(bytes(tr["vec"])))
        nid = tr["node_id"]
        if s > node_trigger_max.get(nid, 0.0):
            node_trigger_max[nid] = s

    frame_rows = conn.execute(
        "SELECT f.id, f.node_id, f.start_off, f.end_off, f.text, f.vec, "
        "n.project_id, n.title, n.state, n.kind, n.updated, p.name AS project_name "
        "FROM pm_frame f "
        "JOIN pm_node n ON n.id=f.node_id "
        "JOIN pm_project p ON p.id=n.project_id "
        "WHERE n.state NOT IN ('dead', 'deprecated')"
    ).fetchall()

    now = time.time()
    tau = recency_tau_days * 86400.0
    scored = []
    for r in frame_rows:
        sim_frame = cosine_similarity(qv, blob_to_array(bytes(r["vec"])))
        sim_trig = node_trigger_max.get(r["node_id"], 0.0)
        base = max(sim_frame, sim_trig)
        age = max(0.0, now - float(r["updated"]))
        recency = recency_lambda * math.exp(-age / tau) if recency_lambda else 0.0
        boost = active_boost if (active_project_id is not None
                                 and r["project_id"] == active_project_id) else 0.0
        scored.append({
            "frame_id": r["id"], "node_id": r["node_id"],
            "start": r["start_off"], "end": r["end_off"],
            "text": r["text"], "title": r["title"],
            "kind": r["kind"], "state": r["state"],
            "project": r["project_name"],
            "score": base + recency + boost,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)

    picked = []
    for cand in scored:
        if len(picked) >= top_k:
            break
        # Non-overlap invariant within same node
        overlap = False
        for p in picked:
            if _intersects(cand["node_id"], cand["start"], cand["end"],
                           p["node_id"], p["start"], p["end"]):
                overlap = True
                break
        if overlap:
            continue
        picked.append(cand)
    return picked


# ── state transitions ───────────────────────────────────────────────────────

KIND_STATES = {
    # north_star lifecycle
    "north_star": {"active", "satisfied"},

    # objective lifecycle
    "objective": {"proposed", "active", "satisfied", "abandoned"},
    # task lifecycle
    "task": {"proposed", "active", "blocked", "done", "abandoned"},
    # decision lifecycle
    "decision": {"proposed", "committed", "superseded", "reverted"},
    # question lifecycle
    "question": {"open", "researching", "answered", "invalidated", "parked"},
    # artifact lifecycle
    "artifact": {"proposed", "active", "deprecated", "removed"},
    # note lifecycle
    "note": {"active", "superseded", "archived"},
}

VALID_TRANSITIONS = {
    # objective
    "proposed":   {"active", "abandoned"},
    "active":     {"satisfied", "abandoned"},
    "satisfied":  set(),
    "abandoned":  set(),
    # task
    "blocked":    {"active", "abandoned"},
    "done":       set(),
    # decision
    "committed":  {"superseded", "reverted"},
    "superseded": set(),
    "reverted":   set(),
    # question
    "open":       {"researching", "answered", "invalidated", "parked"},
    "researching":{"answered", "invalidated", "parked"},
    "answered":   set(),
    "invalidated":set(),
    "parked":     {"open", "researching"},
    # artifact
    "deprecated": {"removed"},
    "removed":    set(),
    # note
    "superseded": {"archived"},
    "archived":   set(),
}


def state_transition(conn, node_id: int, new_state: str) -> dict:
    """Transition a node to a new state with validation. Loud on invalid transitions."""
    row = get_node(conn, node_id)
    if not row:
        raise ValueError(f"node {node_id} not found")
    old_state = row["state"]
    if new_state not in NODE_STATES:
        raise ValueError(f"unknown state {new_state!r}")
    kind = row["kind"]
    allowed_states = KIND_STATES.get(kind)
    if allowed_states and new_state not in allowed_states:
        raise ValueError(f"state {new_state!r} is invalid for kind {kind!r}")
    if old_state == new_state:
        return {"node_id": node_id, "old_state": old_state, "new_state": new_state, "changed": False}
    allowed = VALID_TRANSITIONS.get(old_state, set())
    if new_state not in allowed:
        raise ValueError(
            f"invalid transition: {old_state} -> {new_state}. "
            f"Allowed from {old_state}: {sorted(allowed) or 'terminal'}"
        )
    now = int(time.time())
    conn.execute(
        "UPDATE pm_node SET state=?, updated=? WHERE id=?",
        (new_state, now, node_id),
    )
    conn.commit()
    return {"node_id": node_id, "old_state": old_state, "new_state": new_state, "changed": True}


# ── project compass (status) ─────────────────────────────────────────────────

def project_status(conn, project_id: int, max_bottlenecks: int = 5) -> dict:
    """Walk the active tree and compute frontier, bottlenecks, and summary.

    Returns a one-paragraph compass readout: where we are, what's next,
    what's blocking us.
    """
    # Get all non-terminal nodes with their state counts
    rows = conn.execute(
        "SELECT id, parent_id, kind, state, title, created, updated "
        "FROM pm_node WHERE project_id=? AND state NOT IN ('abandoned', 'deprecated', 'archived', 'removed', 'superseded', 'invalidated', 'reverted') "
        "ORDER BY created",
        (project_id,),
    ).fetchall()
    
    nodes = [dict(r) for r in rows]
    node_map = {n["id"]: n for n in nodes}
    
    # Build children index
    children_of = {}
    for n in nodes:
        pid = n["parent_id"]
        if pid is not None:
            children_of.setdefault(pid, []).append(n["id"])
    
    # Count by state + kind
    counts = {"objective": 0, "task": 0, "decision": 0, "question": 0, "artifact": 0, "note": 0, "north_star": 0,
              "proposed": 0, "active": 0, "blocked": 0, "done": 0, "total": len(nodes)}
    for n in nodes:
        s = n["state"]
        if s in counts:
            counts[s] += 1
        k = n["kind"]
        if k in counts:
            counts[k] += 1
    
    # Frontier: active nodes with no active children (leaf active or blocked)
    frontier = []
    for n in nodes:
        if n["state"] == "active" and n["kind"] != "north_star":
            child_ids = children_of.get(n["id"], [])
            active_children = [cid for cid in child_ids if node_map.get(cid, {}).get("state") == "active"]
            if not active_children:
                frontier.append({
                    "id": n["id"],
                    "title": n["title"],
                    "kind": n["kind"],
                    "state": n["state"],
                    "updated": n["updated"],
                })
    
    # Bottlenecks: blocked nodes sorted by staleness (oldest first)
    blocked = [n for n in nodes if n["state"] == "blocked"]
    blocked.sort(key=lambda n: n["updated"])
    bottlenecks = [
        {"id": n["id"], "title": n["title"], "kind": n["kind"], "updated": n["updated"]}
        for n in blocked[:max_bottlenecks]
    ]
    
    # Ideas: potential that hasn't been activated yet
    ideas = [n for n in nodes if n["state"] == "proposed" and n["kind"] == "objective"]
    proposed_objectives = len(ideas)
    
    # Compute summary sentence
    in_flight = counts["active"] + counts["blocked"]
    done = counts.get("done", 0)
    total_touchable = done + in_flight + proposed_objectives
    
    # North star status
    ns_nodes = [n for n in nodes if n["kind"] == "north_star"]
    if ns_nodes:
        ns_state = ns_nodes[0]["state"]
        summary_parts = [f"North star: {ns_state}"]
    else:
        summary_parts = ["North star: missing"]
    summary_parts.append(f"Project: {done}/{total_touchable} nodes done ({done} done, {in_flight} in flight, {proposed_objectives} objectives)")
    
    if bottlenecks:
        oldest = bottlenecks[0]
        age_days = (int(time.time()) - oldest["updated"]) // 86400
        oldest_title = oldest['title'][:60].replace('"', "'")
        summary_parts.append(f'{len(blocked)} blocked, oldest: "{oldest_title}" (stale {age_days}d)')
    else:
        summary_parts.append("0 blocked")
    
    if frontier:
        summary_parts.append(f"frontier: {len(frontier)} leaf-active nodes")
    elif counts.get("active", 0) == 0:
        summary_parts.append("no active work (everything done or proposed)")
    else:
        summary_parts.append(f"{counts['active']} active (frontier unclear — check tree)")
    
    return {
        "project_id": project_id,
        "counts": counts,
        "frontier": frontier,
        "bottlenecks": bottlenecks,
        "ideas": [{"id": n["id"], "title": n["title"], "kind": n["kind"]} for n in ideas[:10]],
        "summary": " — ".join(summary_parts),
    }


# ── safety: delete node with confirmation ─────────────────────────────────

def delete_node(conn, node_id: int, confirmation_token: str, expected_title: str = "") -> dict:
    '''Delete a node. Requires confirmation_token == "DELETE-{node_id}-{title_slug}".
    Also accepts expected_title as extra validation. Fails loud if node not found.'''
    row = get_node(conn, node_id)
    if not row:
        raise ValueError(f"node {node_id} not found")
    title_slug = re.sub(r'[^a-zA-Z0-9]', '-', row['title'].lower())[:40]
    expected_token = f"DELETE-{node_id}-{title_slug}"
    if confirmation_token.strip() != expected_token:
        raise ValueError(f"confirmation mismatch: expected {expected_token}, got {confirmation_token.strip()}")
    if expected_title and row['title'][:60] != expected_title[:60]:
        raise ValueError(f"title mismatch: expected {expected_title[:60]!r}, got {row['title'][:60]!r}")
    
    # Save old row for backup
    old_row = dict(row)
    # Delete transactionally: frames first, then triggers, then node
    conn.execute("DELETE FROM pm_frame WHERE node_id=?", (node_id,))
    conn.execute("DELETE FROM pm_trigger WHERE node_id=?", (node_id,))
    conn.execute("DELETE FROM pm_conflict WHERE node_a=? OR node_b=?", (node_id, node_id))
    conn.execute("DELETE FROM pm_node WHERE id=?", (node_id,))
    conn.commit()
    return {"deleted": node_id, "title": old_row.get('title', '')}


# ── safety: project dedup on create ───────────────────────────────────────

def _slugify(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '', name.lower())

def get_or_create_project_dedup(conn, name: str) -> tuple[int, bool]:
    '''Like get_or_create_project but checks for slug-duplicate first.
    Returns (project_id, was_created).'''
    name = _sanitize_project_name(name)
    row = conn.execute("SELECT id FROM pm_project WHERE name=?", (name,)).fetchone()
    if row:
        return row[0], False
    
    # Check slug dedup
    slug = _slugify(name)
    existing = conn.execute("SELECT id, name FROM pm_project").fetchall()
    for ex_id, ex_name in existing:
        if _slugify(ex_name) == slug:
            return ex_id, False  # Return existing, don't create dup
    
    cur = conn.execute("INSERT INTO pm_project (name, created) VALUES (?, ?)",
                       (name, int(time.time())))
    conn.commit()
    return cur.lastrowid, True


# ── safety: reparent node (move under new parent) ─────────────────────────

def reparent_node(conn, node_id: int, new_parent_id: int) -> dict:
    '''Move a node under a new parent. Validates both exist and belong to same project.'''
    node = get_node(conn, node_id)
    if not node:
        raise ValueError(f"node {node_id} not found")
    parent = get_node(conn, new_parent_id)
    if not parent:
        raise ValueError(f"parent {new_parent_id} not found")
    if node['project_id'] != parent['project_id']:
        raise ValueError(f"cross-project reparent: node in project {node['project_id']}, parent in {parent['project_id']}")
    if node_id == new_parent_id:
        raise ValueError("cannot reparent a node to itself")
    # Check for cycles: walk up from new_parent, ensure we don't hit node_id
    cursor = new_parent_id
    visited = set()
    while cursor:
        if cursor in visited:
            break
        visited.add(cursor)
        if cursor == node_id:
            raise ValueError("cycle detected: new parent is a descendant of the node")
        p = get_node(conn, cursor)
        cursor = p['parent_id'] if p and p['parent_id'] else None
    
    now = int(time.time())
    conn.execute("UPDATE pm_node SET parent_id=?, updated=? WHERE id=?", (new_parent_id, now, node_id))
    conn.commit()
    return {"node_id": node_id, "old_parent": node['parent_id'], "new_parent": new_parent_id}
