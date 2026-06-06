"""
project filter -- hermes_integration layer.

Wraps raw swiszmem /project/* HTTP endpoints with a living-tree filter
-- so CADEN only sees current, relevant nodes by default.

Dead/superseded/deprecated nodes are hidden unless show_all=True is passed.
This does NOT mutate the core project engine -- it's a pure integration layer.
"""

import httpx
import json
from typing import Any

# Nodes in these states are part of the living tree and shown by default.
LIVING_STATES = {
    'proposed',
    'active',
    'blocked',
    'done',
    'satisfied',
    'committed',
    'open',
    'researching',
    'answered',
    'parked',
}

# These are dead/terminal states -- only shown when show_all=True.
# Every NODE_STATE belongs to exactly one of these two sets.
DEAD_STATES = {
    'deprecated',
    'abandoned',
    'superseded',
    'archived',
    'removed',
    'invalidated',
    'reverted',
}

# Verify the partition is complete (run once at import).
_ALL = LIVING_STATES | DEAD_STATES
_EXPECTED = {'proposed','active','blocked','done','abandoned','deprecated','committed','superseded',
             'reverted','open','researching','answered','invalidated','parked','removed','archived','satisfied'}
assert _ALL == _EXPECTED, f"Partition drift: LIVING + DEAD != full NODE_STATES. Missing: {_EXPECTED - _ALL}"

BASE = "http://127.0.0.1:7437"


def _filter_nodes(nodes: list[dict], show_all: bool = False) -> list[dict]:
    """Return only living nodes unless show_all is true."""
    if show_all:
        return nodes
    return [n for n in nodes if n.get('state') in LIVING_STATES]


def project_status(project: str, show_all: bool = False) -> str:
    """Project compass -- filtered to living tree unless show_all.
    Returns JSON string with counts, frontier, bottlenecks, and summary."""
    try:
        r = httpx.post(f"{BASE}/project/status", json={"project": project}, timeout=10)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return json.dumps({"error": str(e)})

    if not show_all:
        d['frontier'] = _filter_nodes(d['frontier'])
        d['bottlenecks'] = _filter_nodes(d['bottlenecks'])
        d['ideas'] = _filter_nodes(d['ideas'])

        fm = d['frontier']
        counts = d['counts']
        summary_parts = [
            f"Project: 0 dead, {counts.get('done',0)} done, {counts.get('active',0)+counts.get('blocked',0)} in flight"
        ]
        if d['bottlenecks']:
            summary_parts.append(f"{len(d['bottlenecks'])} blocked")
        else:
            summary_parts.append("0 blocked")
        if fm:
            summary_parts.append(f"frontier: {len(fm)} leaf-active nodes")
        d['summary'] = " -- ".join(summary_parts)

    return json.dumps(d, default=str)


def project_tree(project: str, show_all: bool = False) -> str:
    """Project tree -- filtered to living tree unless show_all. Returns JSON string."""
    try:
        r = httpx.post(f"{BASE}/project/tree", json={"project": project}, timeout=10)
        r.raise_for_status()
        nodes = r.json().get('nodes', r.json())
    except Exception as e:
        return json.dumps({"error": str(e)})

    if not show_all:
        nodes = _filter_nodes(nodes)

    # Warn loudly about stale-pinned nodes
    stale_nodes = []
    for n in nodes:
        raw_tags = n.get("tags", "[]")
        try:
            tag_list = json.loads(raw_tags) if isinstance(raw_tags, str) else raw_tags
        except Exception:
            tag_list = []
        if "stale_pin" in tag_list:
            stale_nodes.append({"id": n.get("id"), "title": n.get("title", "")[:80]})

    result = {"nodes": nodes, "filtered": not show_all, "total": len(nodes)}
    if stale_nodes:
        result["STALE_PIN_WARNING"] = (
            f"{len(stale_nodes)} node(s) have unverified AST pin claims — "
            "the code they describe no longer matches. Run `ast pin verify NID` to re-check."
        )
        result["stale_pinned_nodes"] = stale_nodes
    return json.dumps(result, default=str)


def project_list() -> str:
    """List all projects with living-node counts."""
    try:
        r = httpx.get(f"{BASE}/project/list", timeout=10)
        r.raise_for_status()
        projects = r.json().get('projects', [])
    except Exception as e:
        return json.dumps({"error": str(e)})

    for p in projects:
        try:
            r2 = httpx.post(f"{BASE}/project/tree", json={"project": p['name']}, timeout=10)
            r2.raise_for_status()
            all_nodes = r2.json().get('nodes', r2.json())
            living = _filter_nodes(all_nodes)
            p['living_nodes'] = len(living)
            p['total_nodes'] = len(all_nodes)
            p['dead_nodes'] = len(all_nodes) - len(living)
        except Exception:
            p['living_nodes'] = p.get('node_count', 0)
            p['total_nodes'] = p.get('node_count', 0)
            p['dead_nodes'] = 0

    return json.dumps({"projects": projects}, default=str)

