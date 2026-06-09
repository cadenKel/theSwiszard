"""
cli.py — swiszcli: thin MCP client REPL.

Architecture (post-pivot 2026-06-08):
  1. OBSERVE  — model fills structured situation block (observations only)
  2. INJECT   — three sources:
                  swiszcontext: overlapping conversation frames (cosine × recency, last-3 pinned)
                  swiszproj:    PM tree nodes (pm_orient — mandatory at session start)
                  swiszmem:     long-term memories (recall_triggers)
  3. DELIBERATE — looking_glass × 2 (thought → trigger-matched memories → refine)
  4. ACT      — every tool call prefaced with WHY; consequence stored in swiszmem

The model talks to the swiszard MCP server at :8743 — same surface Hermes uses.
No bespoke dispatch, no router, no wizard machinery.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

from .config import Config
from .display import print_token, print_tool_call, print_tool_result, print_session_header, print_turn_header, print_error, print_phase, print_situation, print_glass, print_phase_error
from .session_logger import SessionLogger

# ── MCP client ────────────────────────────────────────────────────────────────

MCP_URL = "http://127.0.0.1:8743/mcp/"
MEM_URL = "http://127.0.0.1:7437"

# ── colours ───────────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD  = "\033[1m"
CYAN  = "\033[36m"
GREY  = "\033[90m"
YELLOW = "\033[33m"
RED   = "\033[31m"
GREEN = "\033[32m"


def _c(s: str, code: str) -> str:
    return f"{code}{s}{RESET}"





# ── swiszmem HTTP helpers (direct, not through MCP) ───────────────────────────

def _mem_post(endpoint: str, payload: dict) -> dict:
    """Synchronous POST to swiszmem. Used for context frames + looking_glass."""
    try:
        r = httpx.post(f"{MEM_URL}{endpoint}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def context_append(session_id: str, turn: int, role: str, content: str) -> None:
    _mem_post("/context/append", {
        "session_id":  session_id,
        "turn_number": turn,
        "role":        role,
        "content":     content,
    })


def context_recall(session_id: str, situation_text: str, top_k: int = 5) -> list[dict]:
    resp = _mem_post("/context/recall", {
        "session_id":     session_id,
        "situation_text": situation_text,
        "top_k":          top_k,
    })
    return resp.get("frames", [])


def glass_consult(thought: str, top_k: int = 5) -> str:
    resp = _mem_post("/glass/consult", {"thought": thought, "top_k": top_k})
    return resp.get("formatted", "")


def glass_store_consequence(why: str, tool_name: str, result_summary: str,
                             session_id: str, turn: int) -> None:
    _mem_post("/glass/store_consequence", {
        "why":            why,
        "tool_name":      tool_name,
        "result_summary": result_summary,
        "session_id":     session_id,
        "turn":           turn,
    })


def mem_recall_triggers(session_id: str, situation_text: str, top_k: int = 8) -> list[dict]:
    resp = _mem_post("/recall_triggers", {
        "query":      situation_text,
        "session_id": session_id,
        "top_k":      top_k,
    })
    return resp.get("memories", [])


# ── MCP tool call ─────────────────────────────────────────────────────────────

async def mcp_call(tool: str, args: dict) -> str:
    """Call a tool on the swiszard MCP server. Returns text content."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/home/ziggibot/theSwiszard/.venv/lib/python3.12/site-packages")
        from fastmcp import Client
        async with Client(MCP_URL) as client:
            result = await client.call_tool(tool, args)
            if hasattr(result, "content"):
                parts = result.content
                if parts:
                    return parts[0].text if hasattr(parts[0], "text") else str(parts[0])
            return str(result)
    except Exception as exc:
        return f"[mcp error] {exc}"


def mcp_call_sync(tool: str, args: dict) -> str:
    return asyncio.run(mcp_call(tool, args))


# ── system prompt builder ─────────────────────────────────────────────────────

SYSTEM_BASE = """\
You are swiszcli — a local AI assistant running on the swiszard system.

HARD RULES:
1. Before every tool call you MUST state WHY in one sentence (prefix: WHY:).
2. Write PM nodes for stated intent + decisions. Not for every micro-action.
3. Fail loud. Do not invent fallbacks. If something is missing, say so.

TOOLS AVAILABLE (via swiszard MCP at :8743):
  swiszard_do          {{"task": "run: ls /tmp"}}
  pm_orient            {{"project": "swiszard", "root_id": 41, "query": ""}}
  pm_add               {{"project": "swiszard", "body": "...", "kind": "task", "state": "active", "parent_id": 0, "title": "..."}}
  pm_safe_transition   {{"node_id": 123, "state": "done"}}
  pm_complete          {{"node_id": 123, "file_path": "/path/to/file.py", "func_name": "my_func"}}
  pm_kill              {{"node_id": 123, "reason": "..."}}
  pm_node              {{"node_id": 123}}
  pm_tree              {{"project": "swiszard"}}
  pm_subtree           {{"project": "swiszard", "root_id": 123}}
  pm_status            {{"project": "swiszard"}}
  pm_list              {{}}
  swiszard_patch_and_verify  {{"file_path": "/path/file.py", "old_str": "...", "new_str": "..."}}
  swiszard_service_logs      {{"service": "swiszmem", "n": 30}}

HOW TO CALL A TOOL — emit EXACTLY this format, one block per tool call:
  WHY: <one sentence explaining why you are calling this tool>
  TOOL: <tool_name> <json_args_on_one_line>

Example:
  WHY: need to see the current PM frontier before proposing new work
  TOOL: pm_orient {{"project": "swiszard", "root_id": 41, "query": "session start"}}

Rules:
- WHY must come immediately before its TOOL line
- JSON args must be valid JSON on a single line
- Multiple tool calls: repeat WHY/TOOL block for each one
- Never call a tool without WHY

Before responding you will be asked to fill a SITUATION block three times. The third fill is used to instruct your response. Fill it honestly — it is a structured thinking packet, not a status report.
"""

SYSTEM_PROMPT = SYSTEM_BASE

SITUATION_TEMPLATE = """\
<SITUATION>
project:      {project}
user_intent:  {user_intent}
model_intent: {model_intent}
recent_tools: {recent_tools}
pm_nodes:     {pm_nodes}
working_file: {working_file}
</SITUATION>"""

_SITUATION_BLANK = SITUATION_TEMPLATE.format(
    project="?", user_intent="?", model_intent="?",
    recent_tools="?", pm_nodes="?", working_file="?",
)

_FILL_PROMPT = """\
Fill the SITUATION block below. Output ONLY the filled <SITUATION>...</SITUATION> block — no prose, no tool calls.

Fields:
  project      — name of the active project
  user_intent  — what you believe the user is actually asking for, in your own words
  model_intent — what you plan to do about it this turn
  recent_tools — comma-separated tools called in recent turns, or "none"
  pm_nodes     — comma-separated active PM node IDs from context, or "none"
  working_file — file you expect to touch this turn, or "none"

[CONTEXT]
{injection}

[USER MESSAGE]
{user_msg}

[RECENT TOOLS]
{recent_tools}

{blank}"""

_RECONSIDER_PROMPT = """\
You filled the SITUATION block below to describe your perceived situation and intent.
The looking_glass retrieved this from past sessions where a similar situation occurred:

[LOOKING GLASS]
{glass}

Reconsider your SITUATION block in light of this — specifically user_intent and model_intent.
You are allowed to keep your original answer if you still believe it is correct.
Only revise if the glass warning reveals a genuine problem with your current framing.

Output ONLY the updated (or unchanged) <SITUATION>...</SITUATION> block. No prose. No explanation.

[YOUR CURRENT SITUATION]
{situation}"""


# ── LLM call (Ollama / OpenAI-compat) ────────────────────────────────────────

def _llm_stream(messages: list[dict], cfg: Config) -> str:
    """Stream from Ollama or OpenAI-compat endpoint. Returns full text."""
    import httpx as _httpx

    payload = {
        "model":    cfg.model,
        "messages": messages,
        "stream":   True,
    }

    url = cfg.provider_base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.provider != "ollama":
        headers["Authorization"] = f"Bearer {cfg.provider_api_key}"

    buf = []
    with _httpx.stream("POST", url, json=payload, headers=headers,
                        timeout=_httpx.Timeout(connect=10.0, read=600.0,
                                               write=10.0, pool=5.0)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            line = line.strip()
            if not line or line == "data: [DONE]":
                continue
            if line.startswith("data: "):
                line = line[6:]
            try:
                chunk = json.loads(line)
                tok = chunk["choices"][0]["delta"].get("content", "")
                if tok:
                    print_token(tok)
                    buf.append(tok)
            except Exception:
                pass
    print()
    return "".join(buf)


# ── injection builder ─────────────────────────────────────────────────────────

def build_injection(
    *,
    session_id: str,
    turn: int,
    situation_text: str,
    pm_orient_text: str = "",
    glass_text: str = "",
    top_k_context: int = 5,
    top_k_mem: int = 8,
) -> str:
    """Build the three-source injection block for a turn."""
    parts = []

    # 1. swiszcontext frames
    frames = context_recall(session_id, situation_text, top_k_context)
    if frames:
        frame_lines = [f"[context_frames count={len(frames)}]"]
        for f in frames:
            pin = " [PINNED]" if f.get("pinned") else ""
            frame_lines.append(
                f"  frame {f['frame_index']} turns {f['turn_start']}-{f['turn_end']}"
                f" score={f.get('score','?')}{pin}"
            )
            frame_lines.append(f"  {f['text'][:300]}")
        parts.append("\n".join(frame_lines))

    # 2. swiszproj PM nodes (already fetched via pm_orient — passed in)
    if pm_orient_text:
        parts.append(f"[pm_orient]\n{pm_orient_text[:1500]}")

    # 3. swiszmem long-term memories
    mems = mem_recall_triggers(session_id, situation_text, top_k_mem)
    if mems:
        mem_lines = [f"[swiszmem memories count={len(mems)}]"]
        for m in mems:
            mem_lines.append(f"  [{m.get('id','?')}] {m.get('content','')[:200]}")
        parts.append("\n".join(mem_lines))

    # deliberation block
    if glass_text:
        parts.append(glass_text)

    return "\n\n".join(parts)


# ── harness-enforced turn phases ─────────────────────────────────────────────

def _harness_orient(session_id: str, turn: int, user_msg: str, pm_orient_cache: str) -> str:
    """
    Phase 1 — ORIENT (zero model calls).
    Calls pm_orient (via cache), swiszmem recall, and context frames.
    situation_text is the raw user message — used only for memory/context retrieval.
    Returns assembled injection block (no glass yet — glass fires after SITUATION fill).
    """
    return build_injection(
        session_id=session_id,
        turn=turn,
        situation_text=user_msg,
        pm_orient_text=pm_orient_cache,
        glass_text="",   # glass fires in phase 3, after model fills SITUATION
    )


def _llm_fill(prompt: str, cfg: "Config") -> str:
    """Single non-streaming model call. Returns content string or raises."""
    import httpx as _httpx
    url = cfg.provider_base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.provider != "ollama":
        headers["Authorization"] = f"Bearer {cfg.provider_api_key}"
    r = _httpx.post(
        url,
        json={"model": cfg.model, "messages": [{"role": "user", "content": prompt}], "stream": False},
        headers=headers,
        timeout=_httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=5.0),
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


_RECONSIDER_PROMPT = """\
You just described your intent in the SITUATION block below.
The looking_glass retrieved the following from past sessions where a similar situation occurred:

[LOOKING GLASS]
{glass}

Reconsider your SITUATION block in light of this.
You are allowed to keep your original answer if you still believe it is correct — do not change it just because glass found something.
Only revise if the glass warning reveals a genuine problem with your current intent.

Output ONLY the updated (or unchanged) <SITUATION>...</SITUATION> block. No prose. No explanation.

[YOUR CURRENT SITUATION]
{situation}"""


def _deliberate(user_msg: str, injection: str, recent_tools: list, cfg: "Config") -> tuple[str, str]:
    """
    Three situation fills. Two glass passes between them.
    S1 = initial fill
    G1 = glass(S1) → model reconsiders → S2
    G2 = glass(S2) → model reconsiders → S3 (final intent, injected into main call)
    Returns (s3, glass_text). Both empty on failure — non-fatal but loud.
    """
    fill_prompt = _FILL_PROMPT.format(
        injection=injection[:3000],
        user_msg=user_msg,
        recent_tools=", ".join(recent_tools[-8:]) or "none",
        blank=_SITUATION_BLANK,
    )
    try:
        print_phase("situation fill 1/3")
        s1 = _llm_fill(fill_prompt, cfg)
        print_situation(s1, "S1")

        print_phase("glass pass 1")
        g1 = glass_consult(s1)
        print_glass(1, g1 if g1 else "(no memories matched)")

        s2 = _llm_fill(_RECONSIDER_PROMPT.format(glass=g1 or "(none)", situation=s1), cfg) if g1 else s1
        if g1:
            print_situation(s2, "S2 (after reconsider)")

        print_phase("glass pass 2")
        g2 = glass_consult(s2)
        print_glass(2, g2 if g2 else "(no memories matched)")

        s3 = _llm_fill(_RECONSIDER_PROMPT.format(glass=g2 or "(none)", situation=s2), cfg) if g2 else s2
        if g2:
            print_situation(s3, "S3 (final intent)")

    except Exception as exc:
        print_phase_error("deliberate", str(exc))
        return "", ""

    parts = []
    if g1:
        parts.append(f"[looking_glass pass 1]\n{g1}")
    if g2:
        parts.append(f"[looking_glass pass 2]\n{g2}")
    return s3, "\n\n".join(parts)


# ── REPL ──────────────────────────────────────────────────────────────────────

def _preflight(cfg: Config) -> None:
    """
    Print a status box before the REPL starts.
    Shows: ollama reachability, configured model, VRAM state (hot/cold/unknown).
    Fails loud if ollama is unreachable — don't silently proceed into a broken session.
    """
    import httpx as _httpx

    W     = "\033[97m"
    DIM   = "\033[2m"
    OK    = "\033[32m"
    WARN  = "\033[33m"
    ERR   = "\033[31m"
    R     = "\033[0m"
    B     = "\033[1m"

    def row(label: str, value: str, color: str = W) -> None:
        print(f"  {DIM}{label:<18}{R}{color}{value}{R}")

    print(f"\n{B}{'─'*52}{R}")
    print(f"  {B}swiszcli{R}  {DIM}pre-flight check{R}")
    print(f"{B}{'─'*52}{R}")

    # ── ollama reachability ───────────────────────────────────────────────────
    base = cfg.ollama_url if cfg.provider == "ollama" else None
    if base:
        try:
            r = _httpx.get(f"{base}/api/tags", timeout=4)
            r.raise_for_status()
            available = [m["name"] for m in r.json().get("models", [])]
            row("ollama", f"reachable  ({len(available)} models)", OK)
        except Exception as exc:
            row("ollama", f"UNREACHABLE — {exc}", ERR)
            print(f"{B}{'─'*52}{R}\n")
            raise SystemExit("cannot reach ollama — aborting") from exc

        # ── model status ──────────────────────────────────────────────────────
        model = cfg.model
        if model in available:
            row("model", model, OK)
        else:
            row("model", f"{model}  [NOT FOUND in ollama]", ERR)
            print(f"  {WARN}available:{R}")
            for m in available[:8]:
                print(f"    {DIM}{m}{R}")
            print(f"{B}{'─'*52}{R}\n")
            raise SystemExit(f"model {model!r} not found — set SWISZCLI_MODEL")

        # ── VRAM state ────────────────────────────────────────────────────────
        try:
            ps = _httpx.get(f"{base}/api/ps", timeout=4).json()
            loaded = {m["name"] for m in ps.get("models", [])}
            if model in loaded:
                vram = next(m.get("size_vram", 0)
                            for m in ps["models"] if m["name"] == model)
                gb = vram / 1e9
                row("vram state", f"HOT  ({gb:.1f} GB loaded)", OK)
            else:
                row("vram state", "cold — warming up...", WARN)
                import sys as _sys; _sys.stdout.flush()
                try:
                    _httpx.post(
                        f"{base}/api/generate",
                        json={"model": model, "prompt": "", "stream": False,
                              "options": {"num_predict": 0}},
                        timeout=120,
                    )
                    row("vram state", "HOT  (loaded)", OK)
                except Exception as _warm_exc:
                    row("vram state", f"warm-up failed — {_warm_exc}", ERR)
        except Exception:
            row("vram state", "unknown", DIM)
    else:
        row("provider", cfg.provider, OK)
        row("model", cfg.model, OK)

    # ── swiszmem ──────────────────────────────────────────────────────────────
    try:
        r = _httpx.get(f"{MEM_URL}/health", timeout=3)
        h = r.json()
        status = "ok" if h.get("ok") else h.get("status", "?")
        row("swiszmem", f"reachable  (v{h.get('version','?')}  {status})", OK)
    except Exception as exc:
        row("swiszmem", f"unreachable — {exc}", ERR)

    # ── MCP server ────────────────────────────────────────────────────────────
    try:
        _httpx.get(MCP_URL.rstrip("/") + "/", timeout=3)
        row("swiszard MCP", "reachable", OK)
    except Exception as exc:
        row("swiszard MCP", f"unreachable — {exc}", ERR)

    print(f"{B}{'─'*52}{R}\n")


def banner(cfg: Config, session_id: str) -> None:
    print_session_header(session_id, cfg.model, [])
    print(_c("  MCP → :8743  mem → :7437  /help for commands", GREY))
    print()


def make_input_session(cfg: Config) -> PromptSession:
    hist_path = cfg.state_dir / "history.txt"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(hist_path)),
        style=Style.from_dict({"prompt": "bold cyan"}),
    )


def main() -> None:
    cfg = Config()
    cfg.ensure_dirs()

    session_id = "swisz_" + uuid.uuid4().hex[:8]
    turn       = 0
    _t0        = time.time()
    _n_tools   = 0
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    recent_tools: list[str] = []
    pm_orient_cache: str = ""
    _resume_sid: str | None = None

    # ── parse --resume SID ────────────────────────────────────────────────────
    if "--resume" in sys.argv:
        _idx = sys.argv.index("--resume")
        if _idx + 1 < len(sys.argv):
            _resume_sid = sys.argv[_idx + 1]
            if not _resume_sid.startswith("swisz_"):
                _resume_sid = "swisz_" + _resume_sid

    if _resume_sid:
        import sqlite3 as _sqlite3
        _db_path = cfg.state_dir / "sessions.db"
        if not _db_path.exists():
            print(_c(f"[resume] sessions.db not found at {_db_path}", RED))
            raise SystemExit(1)
        _conn_r = _sqlite3.connect(str(_db_path))
        _rows = _conn_r.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY ts",
            (_resume_sid,),
        ).fetchall()
        _conn_r.close()
        if not _rows:
            print(_c(f"[resume] session '{_resume_sid}' not found — check: swiszcli-swisz-log list", RED))
            raise SystemExit(1)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for _role, _content in _rows:
            messages.append({"role": _role, "content": _content})
        turn = sum(1 for m in messages if m["role"] == "assistant")

    logger = SessionLogger(cfg.state_dir, session_id)
    _preflight(cfg)
    banner(cfg, session_id)

    if _resume_sid:
        n_restored = len(messages) - 1  # exclude system prompt
        print(_c(f"  resumed from {_resume_sid} — {n_restored} messages restored", CYAN))
        print()

    # ── mandatory pm_orient at session start ──────────────────────────────────
    print(_c("orienting against PM tree...", GREY))
    pm_orient_cache = mcp_call_sync("pm_orient", {"project": "swiszard", "root_id": 41, "query": "session start"})
    print(_c(f"PM oriented ({len(pm_orient_cache)} chars)", GREEN))
    print()

    inp = make_input_session(cfg)

    while True:
        try:
            raw = inp.prompt(ANSI(f"\033[1;36myou>\033[0m "))
        except (KeyboardInterrupt, EOFError):
            print(_c("\nbye", GREY))
            break

        raw = raw.strip()
        if not raw:
            continue
        if raw in ("/exit", "/quit", "exit", "quit"):
            break
        # ── slash commands ────────────────────────────────────────────────────
        if raw.startswith("/"):
            _handle_slash(raw, cfg=cfg, session_id=session_id,
                          pm_orient_cache=pm_orient_cache, logger=logger)
            continue

        # ── observe: store user turn in context frames + log ─────────────────
        context_append(session_id, turn, "user", raw)
        logger.log_message("user", raw)

        # ── phase 1: ORIENT (harness, zero model calls) ───────────────────────
        t_orient = time.time()
        print_phase("orient")
        try:
            injection = _harness_orient(session_id, turn, raw, pm_orient_cache)
        except Exception as exc:
            injection = ""
            logger.log_tool_call(handler="harness_orient", task=raw[:200],
                                 result="", duration_ms=0, error=str(exc))
        logger.log_tool_call(handler="harness_orient", task=raw[:200],
                             result=f"injection={len(injection)}chars",
                             duration_ms=int((time.time() - t_orient) * 1000),
                             error=None)

        # ── phase 2+3: SITUATION FILL + GLASS CONSULT ─────────────────────────
        t_delib = time.time()
        try:
            filled_situation, glass_text = _deliberate(raw, injection, recent_tools, cfg)
        except Exception as exc:
            filled_situation, glass_text = "", ""
            logger.log_tool_call(handler="deliberate", task=raw[:200],
                                 result="", duration_ms=0, error=str(exc))
        else:
            logger.log_tool_call(handler="deliberate", task=raw[:200],
                                 result=f"situation={len(filled_situation)}chars glass={len(glass_text)}chars",
                                 duration_ms=int((time.time() - t_delib) * 1000),
                                 error=None)

        # ── phase 4: ACT — build messages + streaming tool loop ──────────────
        full_injection = "\n\n".join(p for p in [injection, filled_situation, glass_text] if p)
        if full_injection:
            messages.append({"role": "system", "content": f"[CONTEXT INJECTION turn={turn}]\n{full_injection}"})

        messages.append({"role": "user", "content": raw})

        # ── agentic loop: call LLM, execute tools, repeat until no more tools ──
        from .display import print_turn_header
        print_turn_header(turn)
        t0 = time.time()
        _stream_ok = True
        tool_calls: list = []
        while True:
            print(_c(f"  [{cfg.model}] thinking...", GREY), flush=True)
            try:
                response = _llm_stream(messages, cfg)
            except Exception as exc:
                err_str = str(exc)
                print_error(f"stream error: {err_str}")
                logger.log_tool_call(handler="llm_stream", task=raw[:200],
                                     result="", duration_ms=int((time.time()-t0)*1000),
                                     error=err_str)
                logger.log_message("assistant", f"[ERROR: {err_str}]")
                _stream_ok = False
                break

            messages.append({"role": "assistant", "content": response})
            logger.log_message("assistant", response)
            context_append(session_id, turn, "assistant", response)

            # ── parse WHY + tool calls ─────────────────────────────────────────
            why_text, tool_calls = _parse_tool_calls(response)
            if not tool_calls:
                break  # no more tools — done

            for tool_name, tool_args in tool_calls:
                recent_tools.append(tool_name)
                recent_tools = recent_tools[-8:]
                print_tool_call(f"{tool_name}({json.dumps(tool_args)[:120]})")
                t_start = time.time()
                result = mcp_call_sync(tool_name, tool_args)
                t_tool = time.time() - t_start
                ok = "error" if result.startswith("[mcp error]") else "ok"
                print_tool_result(ok, t_tool, result[:200])
                _n_tools += 1
                logger.log_tool_call(
                    handler=tool_name,
                    task=json.dumps(tool_args)[:300],
                    result=result[:500],
                    duration_ms=int(t_tool * 1000),
                    error=result if ok == "error" else None,
                )
                if why_text:
                    glass_store_consequence(
                        why=why_text,
                        tool_name=tool_name,
                        result_summary=result[:300],
                        session_id=session_id,
                        turn=turn,
                    )
                # feed result back so model can continue
                messages.append({"role": "user",
                                  "content": f"[tool_result:{tool_name}]\n{result}"})
            # loop: call LLM again with tool results injected

        if not _stream_ok:
            continue
        dt = time.time() - t0

        # refresh pm_orient cache if PM tools were used
        if any(t in ("pm_add", "pm_transition", "pm_safe_transition", "pm_complete")
               for t, _ in tool_calls):
            pm_orient_cache = mcp_call_sync("pm_orient", {
                "project": "swiszard", "root_id": 41, "query": ""})

        print(_c(f"\n  [{dt:.1f}s]", GREY))
        turn += 1

    # ── session summary (printed once on every clean exit) ────────────────────
    duration_s = int(time.time() - _t0)
    h, rem     = divmod(duration_s, 3600)
    m, s       = divmod(rem, 60)
    dur_str    = (f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s")
    u_turns    = sum(1 for msg in messages if msg["role"] == "user")

    print()
    print(_c("─" * 52, GREY))
    print(_c(f"  Resume this session with:", BOLD))
    print(_c(f"    swisz --resume {session_id}", CYAN))
    print()
    print(_c(f"  Session:   {session_id}", RESET))
    if _resume_sid:
        print(_c(f"  Resumed:   {_resume_sid}", RESET))
    print(_c(f"  Duration:  {dur_str}", RESET))
    print(_c(f"  Turns:     {turn}  ({u_turns} user, {_n_tools} tool calls)", RESET))
    print(_c("─" * 52, GREY))
    print()


# ── slash command handler ─────────────────────────────────────────────────────

def _handle_slash(line: str, *, cfg: Config, session_id: str,
                   pm_orient_cache: str, logger: "SessionLogger") -> None:
    from .swisz_log_cli import cmd_replay, cmd_list
    cmd = line.lstrip("/").strip().lower()
    if cmd in ("help", "?"):
        print(_c("""
slash commands:
  /help             this message
  /orient           re-run pm_orient and print result
  /glass <thought>  consult looking_glass with a thought
  /mem <query>      recall long-term memories
  /log [SID]        replay current session (or SID) — tool calls + chat interleaved
  /log list         list all recorded sessions
  /status           show session info
  /exit             quit
""", GREY))
    elif cmd == "orient":
        result = mcp_call_sync("pm_orient", {"project": "swiszard", "root_id": 41, "query": ""})
        print(result)
    elif cmd.startswith("glass "):
        thought = line[7:].strip()
        print(glass_consult(thought))
    elif cmd.startswith("mem "):
        query = line[5:].strip()
        mems = mem_recall_triggers(session_id, query)
        for m in mems:
            print(f"  [{m.get('id')}] {m.get('content','')[:200]}")
    elif cmd == "status":
        print(f"  session: {session_id}")
        print(f"  model:   {cfg.model}")
        print(f"  MCP:     {MCP_URL}")
        print(f"  mem:     {MEM_URL}")
    elif cmd == "log list":
        cmd_list()
    elif cmd.startswith("log"):
        # /log          → replay current session
        # /log <SID>    → replay that session
        parts = line.lstrip("/").strip().split(None, 1)
        sid = parts[1] if len(parts) > 1 else session_id
        # flush logger so the file is up to date before replay
        logger._jsonl_fh.flush()
        cmd_replay(sid)
    else:
        print(_c(f"unknown command: {line}", RED))


# ── tool call parser ──────────────────────────────────────────────────────────

def _parse_tool_calls(text: str) -> tuple[str, list[tuple[str, dict]]]:
    """
    Parse WHY: line and TOOL: lines from model response.

    Expected format:
        WHY: <reason for next tool call>
        TOOL: <tool_name> <json_args>

    Returns (why_text, [(tool_name, args_dict), ...])
    """
    why = ""
    calls = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("WHY:"):
            why = line[4:].strip()
        elif line.startswith("TOOL:"):
            rest = line[5:].strip()
            # split on first space to get tool name
            parts = rest.split(None, 1)
            if not parts:
                continue
            tool_name = parts[0]
            args = {}
            if len(parts) > 1:
                try:
                    args = json.loads(parts[1])
                except json.JSONDecodeError:
                    # fallback: treat rest as task= string for swiszard_do
                    args = {"task": parts[1]}
            calls.append((tool_name, args))
    return why, calls


if __name__ == "__main__":
    main()
