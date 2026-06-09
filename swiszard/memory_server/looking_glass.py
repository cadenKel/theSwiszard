"""
looking_glass.py — swiszmem.looking_glass

Thought-triggered memory retrieval. Takes a raw thought string, embeds it,
and returns memories whose trigger embeddings are similar — surfacing stored
experience, warnings, and past consequences relevant to the current intent.

Designed for the deliberation loop:
  call 1 (raw thought)    → prime the model with what went wrong before
  call 2 (refined thought) → confirm the refined intent is safe / well-grounded

Each result includes the memory content AND the trigger phrase that matched,
so the model sees: "when you were thinking X, you remembered Y".

Intent-to-consequence tuples (WHY-before-tool-call + result) are stored here
as memories with triggers authored from the stated WHY. This closes the loop:
future similar WHY strings surface past consequences automatically.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .embed import embed, blob_to_array, cosine_similarity
from .embedding_rows import get_active_rows, KIND_WEIGHTS


MIN_SCORE     = 0.40   # discard noise below this threshold
TOP_K_DEFAULT = 5


@dataclass
class GlassResult:
    memory_id:     int
    memory_content: str
    trigger_text:  str      # the specific trigger phrase that matched
    score:         float
    kind:          str      # "raw" or "trigger"
    tags:          list[str]


def consult(conn: sqlite3.Connection, thought: str,
            top_k: int = TOP_K_DEFAULT,
            min_score: float = MIN_SCORE) -> list[GlassResult]:
    """
    Embed thought, scan trigger rows, return top_k GlassResult objects.

    Uses embedding_rows so trigger phrases (kind="trigger") outweigh raw
    content matches (kind="raw") per KIND_WEIGHTS.  Groups by memory_id and
    returns the best-scoring row per memory.
    """
    thought_vec = embed(thought)
    rows        = get_active_rows(conn)

    # score every row, keep best per memory_id
    best: dict[int, tuple[float, Any]] = {}
    for row in rows:
        rvec  = blob_to_array(bytes(row["vector"]))
        cos   = cosine_similarity(thought_vec, rvec)
        weight = KIND_WEIGHTS.get(row["kind"], 0.85)
        score  = cos * weight
        mid    = row["memory_id"]
        if score >= min_score:
            if mid not in best or score > best[mid][0]:
                best[mid] = (score, row)

    ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)[:top_k]

    results = []
    for score, row in ranked:
        import json
        try:
            tags = json.loads(row["tags"]) if row["tags"] else []
        except Exception:
            tags = []
        results.append(GlassResult(
            memory_id=row["memory_id"],
            memory_content=row["content"],
            trigger_text=row["source_text"],
            score=round(score, 4),
            kind=row["kind"],
            tags=tags,
        ))
    return results


def format_for_prompt(results: list[GlassResult]) -> str:
    """
    Render GlassResults as an injection block for the model's context.

    Format: concise, structured. The model sees situation → memory → tags.
    """
    if not results:
        return ""
    lines = ["[looking_glass]"]
    for r in results:
        lines.append(f"  trigger: {r.trigger_text!r}  (score={r.score})")
        lines.append(f"  memory:  {r.memory_content}")
        if r.tags:
            lines.append(f"  tags:    {', '.join(r.tags)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def store_consequence(
    conn: sqlite3.Connection,
    why: str,
    tool_name: str,
    result_summary: str,
    session_id: str,
    turn: int,
    insert_memory_fn,   # callable: (conn, content, triggers, kind, session_id, turn) -> int
) -> int:
    """
    Store a WHY + tool_call + result tuple as a memory with the WHY as trigger.

    This closes the deliberation loop: future calls to consult() with a similar
    WHY string will surface this consequence automatically.

    insert_memory_fn should be the memory server's insert_memory helper so we
    don't duplicate DB write logic here.
    """
    content = (
        f"Tool call: {tool_name}\n"
        f"Reason:    {why}\n"
        f"Result:    {result_summary}"
    )
    triggers = [why]
    memory_id = insert_memory_fn(
        conn=conn,
        content=content,
        triggers=triggers,
        kind="consequence",
        session_id=session_id,
        turn=turn,
    )
    return memory_id
