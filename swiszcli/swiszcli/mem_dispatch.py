"""Deterministic prefix dispatcher for memory verbs (no backticks here)."""
from __future__ import annotations
import re
from typing import Optional

_WRAPPERS = ("task", "run", "tool", "cmd")

def strip_xml_wrapper(task):
    s = (task or "").strip()
    for tag in _WRAPPERS:
        open_tag = "<" + tag + ">"
        close_tag = "</" + tag + ">"
        if s.startswith(open_tag) and s.endswith(close_tag):
            s = s[len(open_tag):-len(close_tag)].strip()
            return s
        m = re.match(r"^<" + tag + r"(?:\s[^>]*)?>(.*?)</" + tag + r">$", s, flags=re.DOTALL)
        if m:
            return m.group(1).strip()
    return s

def _parse_ids(rest):
    toks = [t for t in rest.replace(",", " ").split() if t]
    out = []
    for t in toks:
        if t.lstrip("-").isdigit():
            out.append(int(t))
    return out

def try_memory_dispatch(task, mem):
    if not task:
        return None
    payload = strip_xml_wrapper(task).strip()
    low = payload.lower()
    if not low.startswith("memory "):
        return None
    rest_full = payload[len("memory "):].strip()
    if not rest_full:
        return "memory: missing verb. supported: recall, remember, forget, deprecate, pin, unpin, show, list, status, supersede."
    verb, _, rest = rest_full.partition(" ")
    verb = verb.lower(); rest = rest.strip()
    try:
        if verb == "recall":
            if not rest:
                return "memory recall: needs a query."
            rows = mem.recall_content(rest, top_k=10) or []
            if not rows:
                return "memory recall %r: 0 hits." % rest
            lines = ["memory recall %r: %d hit(s)" % (rest, len(rows))]
            for m in rows:
                mid = m.get("id")
                content = (m.get("content") or "").replace(chr(10), " ")[:160]
                score = m.get("score", "")
                tail = (" score=%.2f" % score) if isinstance(score, (int, float)) else ""
                lines.append("  [memory:%s%s] %s" % (mid, tail, content))
            return chr(10).join(lines)
        if verb == "remember":
            if not rest.strip():
                return "memory remember: needs content."
            # Parse optional inline triggers: "content | triggers: t1; t2; t3"
            triggers = []
            content = rest
            trig_match = re.match(r'^(.+?)\s+\|\s*triggers:\s*(.+)$', rest, flags=re.DOTALL)
            if trig_match:
                content = trig_match.group(1).strip()
                trig_text = trig_match.group(2).strip()
                triggers = [t.strip() for t in re.split(r"[;|]", trig_text) if t.strip()]
            res = mem.remember(content, source="swiszard:memory remember", triggers=triggers)
            if isinstance(res, dict):
                trig_count = res.get("triggers_stored", 0)
                if triggers:
                    return "remembered as memory #%s (%d inline triggers)" % (res.get("memory_id"), trig_count)
                return "remembered as memory #%s (%s triggers)" % (res.get("memory_id"), trig_count)
            return "remembered: %s" % res
        if verb in ("forget", "deprecate", "pin", "unpin"):
            ids = _parse_ids(rest)
            if not ids:
                return "memory %s: needs at least one numeric memory id." % verb
            lines = []; ok = 0
            for mid in ids:
                try:
                    if verb == "forget": mem.forget(mid)
                    elif verb == "deprecate": mem.deprecate(mid, reason="swiszard")
                    elif verb == "pin": mem.pin(mid)
                    elif verb == "unpin": mem.unpin(mid)
                    lines.append("  %s #%s ok" % (verb, mid)); ok += 1
                except Exception as ex:
                    lines.append("  %s #%s FAILED: %s: %s" % (verb, mid, type(ex).__name__, ex))
            return ("memory %s: %d/%d ok" % (verb, ok, len(ids))) + chr(10) + chr(10).join(lines)
        if verb == "show":
            ids = _parse_ids(rest)
            if not ids:
                return "memory show: needs a numeric memory id."
            mid = ids[0]
            try:
                data = mem.show(mid)
            except Exception as ex:
                return "memory show #%s failed: %s: %s" % (mid, type(ex).__name__, ex)
            import json as _json
            return _json.dumps(data, indent=2, ensure_ascii=False)
        if verb == "list":
            try:
                data = mem.list_memories(limit=20) or {}
                rows = data.get("memories", data) if isinstance(data, dict) else data
                rows = rows or []
            except Exception as ex:
                return "memory list failed: %s: %s" % (type(ex).__name__, ex)
            if not rows:
                return "memory list: empty."
            lines = ["memory list: %d (most recent first)" % len(rows)]
            for m in rows[:20]:
                mid = m.get("id")
                content = (m.get("content") or "").replace(chr(10), " ")[:120]
                pinned = "*" if m.get("pinned") else " "
                dep = "x" if m.get("deprecated") else " "
                lines.append("  [%s%s] [memory:%s] %s" % (pinned, dep, mid, content))
            return chr(10).join(lines)
        if verb == "status":
            try: return str(mem.status())
            except Exception as ex: return "memory status failed: %s: %s" % (type(ex).__name__, ex)
        if verb == "supersede":
            m = re.match(r"^(\d+)\s+with:\s*(.+)$", rest, flags=re.DOTALL)
            if not m:
                return "memory supersede: usage memory supersede <id> with: <new content>"
            mid = int(m.group(1)); content = m.group(2).strip()
            try:
                mem.supersede(mid, content)
                return "memory supersede #%s ok" % mid
            except Exception as ex:
                return "memory supersede #%s failed: %s: %s" % (mid, type(ex).__name__, ex)
    except Exception as ex:
        return "memory %s crashed: %s: %s" % (verb, type(ex).__name__, ex)
    return "memory %s: unknown verb. supported: recall, remember, forget, deprecate, pin, unpin, show, list, status, supersede." % verb
