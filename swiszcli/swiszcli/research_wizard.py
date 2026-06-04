"""P1.5+P1.6b research wizard: fans out + persists results back to corpus.

Triggered by gap_detector. For each query:
  - swiszard web search (searxng)
  - swizmem recall
  - swiszContext chunk recall
Then writes successful web results BACK into swizmem as new memories so
the void gets filled and we never re-search the same gap.

NO LLM in this module.
"""
from __future__ import annotations


def _short(s, n):
    return (s or "").replace("\n", " ")[:n]


def research(
    queries,
    *,
    swiszard_do,
    mem_recall_triggers=None,
    context_recall_fn=None,
    mem_remember=None,
    session_id="research_wizard",
    proof_loop=None,
    embed_fn=None,
):
    """Fan out research, persist back, return evidence block."""
    if not queries:
        return ""
    sections = []
    for q in queries:
        q_clean = q.strip()
        if not q_clean:
            continue
        section = [f"  [query: {q_clean[:120]}]"]
        web_text = ""
        try:
            web = swiszard_do(f"search the web for {q_clean[:200]}")
            web_text = web or ""
            section.append(f"    web: {_short(web_text, 600)}")
            if proof_loop is not None and embed_fn is not None and web_text.strip():
                try:
                    proof_loop.stash("searxng:research_wizard", embed_fn(web_text[:1500]))
                except Exception:
                    pass
        except Exception as e:
            section.append(f"    web: ERROR {e}")
        if mem_recall_triggers is not None:
            try:
                mems = mem_recall_triggers(q_clean, top_k=3) or []
                for m in mems[:2]:
                    body = (m.get("body") or m.get("text") or "")[:200]
                    section.append(f"    mem[{m.get('id','?')}]: {body}")
                    if proof_loop is not None and embed_fn is not None and body:
                        try:
                            proof_loop.stash(m.get("source") or "swizmem:recall", embed_fn(body))
                        except Exception:
                            pass
            except Exception as e:
                section.append(f"    mem: ERROR {e}")
        if context_recall_fn is not None:
            try:
                hits = context_recall_fn(q_clean) or []
                for h in hits[:2]:
                    if "_error" in h:
                        continue
                    txt = h.get("text", "")[:200]
                    sc = h.get("score", 0.0)
                    section.append(f"    ctx[{h.get('kind','?')} s={sc:.2f}]: {txt}")
                    if proof_loop is not None and embed_fn is not None and txt:
                        try:
                            proof_loop.stash("tool_output", embed_fn(txt))
                        except Exception:
                            pass
            except Exception as e:
                section.append(f"    ctx: ERROR {e}")
        # P1.6b: persist web evidence back to swizmem so corpus grows toward
        # conversation trajectory. Skip if empty or obviously an error.
        if mem_remember is not None and web_text.strip() and not web_text.strip().startswith("ERROR"):
            try:
                content = f"research evidence for query: {q_clean[:150]}\n\n{web_text[:2000]}"
                mem_remember(
                    content,
                    triggers=[q_clean[:120]],
                    kind="research",
                    source="searxng:research_wizard",
                    tags=["auto_research", "void_fill"],
                )
            except Exception as e:
                section.append(f"    persist: ERROR {e}")
        sections.append("\n".join(section))
    if not sections:
        return ""
    out = ["<research_context>"]
    out.extend(sections)
    out.append("</research_context>")
    return "\n".join(out)
