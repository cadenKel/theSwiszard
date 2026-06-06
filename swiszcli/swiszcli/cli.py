# swiszCLI entry point. Type "swisz" (or "swz") in your terminal.
#
# Slash commands are CLIENT-SIDE (never sent to LLM):
#   /help                       this help
#   /quit /exit /q              exit
#   /model [name]               show / set ollama model
#   /ctx                        preview what will be sent to the LLM
#   /sys                        show the system prompt
#   /history                    show turn history
#   /reset                      clear history (memory untouched)
#   /warm                       preload model into VRAM (24h pin)
#   /cold                       evict model from VRAM
#   /stats                      last-turn timing (tok/s, load, prompt, gen)
#   /wiz [name]                 launch a wizard by dotted name
#                               (no name = pick from list)
#   /mem                        → CRUD menu (list/search/remember/edit/deprecate/forget)
#   /mem list                   → last 20 resurfaced memories (numbered)
#   /mem search <query>         → top-20 semantic hits for query (embeds it)
#   /mem edit <N>               → edit memory #N from last list/search
#   /mem show <N>               → full memory dump (content/tags/triggers)
#   /mem forget <N>             → PERMANENT delete memory #N
#   /mem deprecate <N>          → soft delete memory #N
#   /mem pin <N> | /mem unpin <N>
#                                 (rewrite content, add/remove triggers,
#                                  pin/unpin, deprecate, forget)
#   /mem remember               → direct mem.remember wizard
#   /mem <wizard-suffix>        → direct mem.<suffix> wizard
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

from .agent import Agent, AgentState
from .config import Config
from .llm import make_llm
from .memory import MemoryClient
from .prompt import build_memory_block, build_code_context_block, build_system_prompt, build_system_prompt_full
from .swiszard_bridge import SwiszardUnavailable, load_swiszard_do
from .mem_dispatch import try_memory_dispatch, strip_xml_wrapper
from swiszproj.dispatch import try_project_dispatch
from .wizard import Cancelled, list_wizards, resolve
from .wizard_ptk import PTKRunner
from . import wizards_mem, pools, trace as tracelog, archive as toolarchive, wizard_store, wizards_meta, sessions as sessionlog, ctx_budget, swisz_log as swiszcalls
from swiszproj import wizards as wizards_proj
from swiszproj.client import ProjectClient
from .warm import preload, unload, is_resident, Spinner

# P0: swiszContext + router hint wiring (2026-06-01)
from .context_store import ContextStore as _CtxStore
from .router import Router as _Router
from .chunks import ChunkCapture as _ChunkCapture, render_chunks as _render_chunks
from .router_hint import router_hint as _router_hint
from .scratchpad import ScratchpadStore as _SPStore
from .scratchpad_wizards import ScratchpadOps as _SPOps, parse_and_dispatch as _sp_dispatch
from .sequence_learn import SequenceStore as _SeqStore, render_sequence_hint as _render_seq_hint
from swiszproj.state import ProjectStore as _ProjStore, detect_project as _detect_proj
from .edit_engine import EditEngine as _EditEngine
from .ast_index import ASTIndex as _ASTIndex
from .edit_wizards import EditOps as _EditOps, dispatch as _edit_dispatch, _dsl_match as _edit_dsl_match
from . import gap_detector as _gap_detector
from . import research_wizard as _research_wizard
from . import void_detector as _void_detector
from .trajectory import Trajectory as _Trajectory
from .embed import embed as _embed, EmbedError as _EmbedError
from .task_fingerprint import TaskFingerprint as _TaskFingerprint
from .task_fingerprint import blend as _fp_blend
from .speculative import SpeculativeCache as _SpecCache
from .stats import incr as _stats_incr


DIM = "\x1b[2m"
BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
MAGENTA = "\x1b[35m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
RESET = "\x1b[0m"


def c(s: str, color: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"{color}{s}{RESET}"


def banner(cfg: Config, session_id: str) -> None:
    print(c("swiszCLI", BOLD + CYAN) + c(f"  session={session_id}  model={cfg.model}", DIM))
    print(c(f"  swiszard={cfg.swiszard_path}  swizmem={cfg.mem_url}", DIM))
    print(c("  /help for commands  |  /wiz to browse wizards  |  /quit to exit", DIM))
    print()


def render_recall_banner(mems: list) -> None:
    if not mems:
        return
    print(c("  ── recall ──", DIM))
    for m in mems[:5]:
        mid = m.get("id")
        score = m.get("trigger_score", 0)
        trig = m.get("matched_trigger", "?")
        content = (m.get("content") or "").replace("\n", " ")[:100]
        print(c(f"  [mem:{mid}] s={score:.2f} via {trig!r:.40}  {content}", DIM))
    print()


def format_resurfaced_memories(mems: list, *, limit: int = 20) -> list[str]:
    rows = []
    for i, m in enumerate(mems[:limit], 1):
        mid = m.get("id")
        score = m.get("trigger_score", 0.0)
        trig = (m.get("matched_trigger") or "?").replace("\n", " ")
        content = (m.get("content") or "").replace("\n", " ")
        rows.append(
            f"  {i:>2}. [mem:{mid}] s={score:.2f} via {trig[:60]!r}\n"
            f"      {content[:400]}"
        )
    return rows


def print_resurfaced_memories(mems: list, *, limit: int = 20) -> None:
    rows = format_resurfaced_memories(mems, limit=limit)
    if not rows:
        print(c("  no resurfaced memories yet this session", DIM))
        return
    print(c(f"  last {len(rows)} resurfaced memories", DIM))
    for row in rows:
        print(c(row, DIM))


def launch_wizard(name: str, runner: PTKRunner, *, initial: dict | None = None,
                   parent_trace_id: str | None = None) -> None:
    try:
        wiz = resolve(name)
    except KeyError as e:
        print(c(str(e), RED))
        return
    print(c(f"── wizard: {wiz.name} ──", CYAN))
    tw = tracelog.get_default()
    trace_id = tw.start(wiz.name, "launch_wizard", parent_id=parent_trace_id,
                        initial_ctx=initial or {}) if tw else None
    try:
        result = wiz.run(runner, initial=initial)
    except Cancelled:
        if tw and trace_id:
            tw.end(trace_id, initial or {}, None, "cancelled")
        print(c("(cancelled)", YELLOW))
        return
    except Exception as e:
        if tw and trace_id:
            tw.end(trace_id, initial or {}, str(e), "error")
        print(c(f"wizard error: {type(e).__name__}: {e}", RED))
        return
    if tw and trace_id:
        tw.end(trace_id, initial or {}, result, "ok")
    print(c(f"  → {result}", GREEN))


def pick_wizard(runner: PTKRunner) -> str | None:
    from .wizard import Choice, Step, Wizard
    names = list_wizards()
    if not names:
        print("no wizards registered")
        return None
    picker = Wizard(
        name="__picker__",
        title="pick a wizard",
        steps=[Step(
            key="name", kind="pick", prompt="which wizard?",
            choices=lambda c: [Choice(value=n, label=n) for n in names],
        )],
    )
    try:
        result = picker.run(runner)
        return result.get("name") if isinstance(result, dict) else result
    except Cancelled:
        return None



def _print_tree(pc, name: str) -> None:
    """Print a project tree indented by depth."""
    try:
        data = pc.tree(name)
    except Exception as e:
        print(c(f"  tree fetch failed: {e}", RED)); return
    nodes = data.get("nodes", [])
    if not nodes:
        print(c(f"  {name}: empty", DIM)); return
    by_parent: dict = {}
    for n in nodes:
        by_parent.setdefault(n.get("parent_id"), []).append(n)
    def _walk(parent_id, depth):
        for n in by_parent.get(parent_id, []):
            kind = n.get("kind", "?")
            st = n.get("state", "")
            title = (n.get("title") or n.get("body") or "").splitlines()[0][:80]
            print(c("  " + ("  " * depth) + f"[{kind}/{st}] #{n.get('id')} {title}", DIM))
            _walk(n.get("id"), depth + 1)
    _walk(None, 0)

def handle_slash(line: str, *, cfg: Config, mem: MemoryClient, state: AgentState, runner: PTKRunner, llm_for_stats):
    if not line.startswith("/"):
        return None
    parts = line.strip().split()
    cmd = parts[0][1:].lower()
    args = parts[1:]

    if cmd in ("quit", "exit", "q"):
        raise SystemExit(0)
    if cmd == "help":
        print(__doc__)
        return True
    if cmd == "model":
        if not args:
            print(f"model: {cfg.model}")
        else:
            cfg.model = args[0]
            print(f"model set to {cfg.model}")
        return True
    if cmd == "sys":
        print(state.system_prompt)
        return True
    if cmd == "ctx":
        msgs = state.messages_for_model()
        total = sum(len(m["content"]) for m in msgs)
        print(c(f"-- context preview ({len(msgs)} msgs, {total} chars) --", DIM))
        for m in msgs:
            preview = m["content"][:300].replace("\n", " ")
            suffix = "..." if len(m["content"]) > 300 else ""
            print(c(f"[{m["role"]}]", YELLOW), preview + suffix)
        return True
    if cmd == "history":
        for i, t in enumerate(state.history):
            print(c(f"[{i}] {t.role}", YELLOW), t.content[:300].replace("\n", " "))
        return True
    if cmd == "reset":
        state.history.clear()
        print("history cleared (memory untouched)")
        return True

    if cmd == "wiz":
        if not args:
            chosen = pick_wizard(runner)
            if chosen:
                launch_wizard(chosen, runner)
        else:
            launch_wizard(args[0], runner)
        return True

    if cmd in ("project", "proj"):
        # /project (alias /proj) — friction-zero project manager.
        #   /project              -> menu (add idea | use | tree | conflicts | list | new)
        #   /project add          -> proj.add_idea wizard (capture an idea)
        #   /project use [name]   -> set active project
        #   /project tree [name]  -> render project as indented tree
        #   /project conflicts    -> walk open conflicts
        #   /project list         -> list projects
        #   /project new <name>   -> create project
        pc = ProjectClient(cfg.mem_url)
        sub = (args[0].lower() if args else "")
        rest = args[1:]
        try:
            if sub in ("", "menu"):
                from .wizard import Choice, Step, Wizard
                names = [
                    ("add idea",  "proj.add_idea"),
                    ("use",       "proj.use"),
                    ("conflicts", "proj.conflicts"),
                    ("new",       "proj.new"),
                ]
                runner_choices = [Choice(value=n, label=lbl) for lbl, n in names]
                runner_choices.append(Choice(value="__tree__",  label="tree"))
                runner_choices.append(Choice(value="__list__",  label="list"))
                pick = runner.pick("project menu", runner_choices)
                if pick == "__list__":
                    rows = pc.list()
                    if not rows:
                        print(c("  no projects yet", DIM))
                    else:
                        for r in rows:
                            mark = " *" if r.get("name") == wizards_proj.get_active() else ""
                            print(c(f"  #{r.get('id')}  {r.get('name')}{mark}", DIM))
                elif pick == "__tree__":
                    name = wizards_proj.get_active()
                    if not name:
                        print(c("  no active project. /project use first.", YELLOW))
                    else:
                        _print_tree(pc, name)
                elif pick:
                    launch_wizard(pick, runner)
                return True
            if sub in ("add", "idea", "add_idea"):
                launch_wizard("proj.add_idea", runner)
                return True
            if sub == "use":
                if rest:
                    wizards_proj.set_active(rest[0])
                    print(c(f"  active project: {rest[0]}", DIM))
                else:
                    launch_wizard("proj.use", runner)
                return True
            if sub == "new":
                if rest:
                    name = " ".join(rest)
                    out = pc.create(name)
                    wizards_proj.set_active(name)
                    print(c(f"  created #{out.get('id')}  {name} (active)", DIM))
                else:
                    launch_wizard("proj.new", runner)
                return True
            if sub == "list":
                rows = pc.list()
                if not rows:
                    print(c("  no projects yet", DIM)); return True
                for r in rows:
                    mark = " *" if r.get("name") == wizards_proj.get_active() else ""
                    print(c(f"  #{r.get('id')}  {r.get('name')}{mark}", DIM))
                return True
            if sub == "tree":
                name = (rest[0] if rest else wizards_proj.get_active())
                if not name:
                    print(c("  usage: /project tree <name>  (or /project use <name> first)", YELLOW))
                    return True
                _print_tree(pc, name)
                return True
            if sub == "conflicts":
                rows = pc.conflicts(project=wizards_proj.get_active())
                if not rows:
                    print(c("  no open conflicts", DIM)); return True
                launch_wizard("proj.conflicts", runner)
                return True
            if sub == "inject":
                q = " ".join(rest).strip()
                if not q:
                    print(c("  usage: /project inject <query>", YELLOW)); return True
                frames = pc.inject(q, active_project=wizards_proj.get_active())
                if not frames:
                    print(c("  no frames matched", DIM)); return True
                for f in frames:
                    print(c(f"  [n:{f.get('node_id')} f:{f.get('frame_id')}] s={f.get('score',0):.3f}", DIM))
                    print(c(f"      {(f.get('text') or '')[:200]}", DIM))
                return True
            print(c(f"  unknown /project subcommand: {sub}", RED))
        except Exception as e:
            print(c(f"  /project error: {type(e).__name__}: {e}", RED))
        return True

    if cmd == "index":
        # /index — manage project AST indexes for code-aware context injection.
        #   /index               -> menu
        #   /index list          -> show indexed roots + chunk counts
        #   /index add [path]    -> index folder (prompts w/ path completer if omitted)
        #   /index remove [N|path] -> remove by index number from last list, or by path
        #   /index status        -> server stats
        sub = args[0] if args else ""
        if not sub:
            print(c("  /index commands:", DIM))
            print(c("    /index add [path]      add a folder; AST chunks embedded + indexed", DIM))
            print(c("    /index list            list indexed roots", DIM))
            print(c("    /index remove [N|path] remove by list-number or absolute path", DIM))
            print(c("    /index status          server-side stats", DIM))
            return True

        if sub == "list":
            try:
                roots = mem.code_index_list().get("roots", [])
            except Exception as e:
                print(c(f"  error: {e}", RED)); return True
            if not roots:
                print(c("  (no indexed roots)", DIM)); return True
            state.last_index_list = [r["root"] for r in roots]
            for i, r in enumerate(roots, 1):
                ago = int(time.time() - (r.get("last_scan_at") or 0))
                flag = "" if r.get("active") else " [inactive]"
                print(c(f"  {i:2d}. {r['root']}  ({r.get('chunks',0)} chunks, last scan {ago}s ago){flag}", DIM))
            return True

        if sub == "add":
            target = " ".join(args[1:]).strip() if len(args) > 1 else ""
            if not target:
                from prompt_toolkit import prompt as ptk_prompt
                from prompt_toolkit.completion import PathCompleter
                try:
                    target = ptk_prompt(
                        "  folder to index: ",
                        completer=PathCompleter(only_directories=True, expanduser=True),
                        complete_while_typing=True,
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    print(c("  cancelled", DIM)); return True
            if not target:
                print(c("  no path given", RED)); return True
            root = Path(target).expanduser().resolve()
            if not root.is_dir():
                print(c(f"  not a directory: {root}", RED)); return True
            try:
                resp = mem.code_index_add(str(root))
            except Exception as e:
                print(c(f"  index FAILED: {e}", RED)); return True
            print(c(f"  ✓ queued {root} (status: {resp.get('status')})", GREEN))
            print(c("    indexing runs in background; check progress with /index list. Watcher keeps it fresh forever.", DIM))
            return True

        if sub == "remove":
            target = " ".join(args[1:]).strip() if len(args) > 1 else ""
            try:
                roots = mem.code_index_list().get("roots", [])
            except Exception as e:
                print(c(f"  error: {e}", RED)); return True
            if not roots:
                print(c("  no indexed roots to remove", DIM)); return True
            chosen: str | None = None
            if target.isdigit():
                idx = int(target) - 1
                lst = getattr(state, "last_index_list", None) or [r["root"] for r in roots]
                if 0 <= idx < len(lst):
                    chosen = lst[idx]
            elif target:
                if any(r["root"] == target for r in roots):
                    chosen = target
                else:
                    matches = [r["root"] for r in roots if target in r["root"]]
                    if len(matches) == 1:
                        chosen = matches[0]
                    elif len(matches) > 1:
                        print(c(f"  ambiguous, matches: {matches}", RED)); return True
            if not chosen:
                # interactive picker
                print(c("  pick a root to remove:", DIM))
                for i, r in enumerate(roots, 1):
                    print(c(f"    {i}. {r['root']} ({r.get('chunks',0)} chunks)", DIM))
                try:
                    sel = input("  number: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print(c("  cancelled", DIM)); return True
                if not sel.isdigit() or not (1 <= int(sel) <= len(roots)):
                    print(c("  invalid", RED)); return True
                chosen = roots[int(sel) - 1]["root"]
            try:
                res = mem.code_index_remove(chosen)
            except Exception as e:
                print(c(f"  remove FAILED: {e}", RED)); return True
            print(c(f"  ✓ removed {chosen} ({res.get('chunks_deleted',0)} chunks)", GREEN))
            return True

        if sub == "status":
            try:
                roots = mem.code_index_list().get("roots", [])
            except Exception as e:
                print(c(f"  error: {e}", RED)); return True
            total = sum(r.get("chunks", 0) for r in roots)
            print(c(f"  {len(roots)} root(s), {total} total chunks", DIM))
            for r in roots:
                print(c(f"    {r['root']} → {r.get('chunks',0)} chunks", DIM))
            return True

        print(c(f"  unknown /index subcommand: {sub}", RED)); return True

    if cmd == "mem":
        # /mem CRUD subcommands (client-side, never sent to LLM):
        #   /mem                    -> CRUD menu
        #   /mem list               -> last 20 resurfaced memories
        #   /mem search [query]     -> top-20 semantic hits (embeds query)
        #   /mem edit <N>           -> edit memory #N from the last list/search
        #   /mem remember           -> mem.remember wizard
        #   /mem forget             -> mem.forget wizard (PERMANENT)
        #   /mem <wizard-suffix>    -> any mem.* wizard by dotted name
        from .wizard import Choice, Step, Wizard
        names = list_wizards("mem.")

        def _show_numbered(rows, header):
            # rows: list of memory dicts with id+content (+optional trigger_score)
            state.last_mem_list = [r.get("id") for r in rows]
            if not rows:
                print(c(f"  {header}: no results", DIM))
                return
            print(c(f"  {header}", DIM))
            for i, m in enumerate(rows[:20], 1):
                mid = m.get("id")
                score = m.get("trigger_score", m.get("score", 0.0))
                content = (m.get("content") or "").replace("\n", " ")
                tags = m.get("tags") or []
                tagstr = (" " + " ".join(f"#{t}" for t in tags)) if tags else ""
                if score:
                    print(c(f"  {i:>2}. [mem:{mid}] s={score:.2f}{tagstr}", DIM))
                else:
                    print(c(f"  {i:>2}. [mem:{mid}]{tagstr}", DIM))
                print(c(f"      {content[:400]}", DIM))

        def _resolve_n(n_str):
            # Numbers are treated as raw memory IDs (the [mem:NNN] shown in lists).
            # If the user typed a small position (1..len(last_mem_list)) AND that
            # position is not itself a valid mem ID, fall back to position lookup
            # for backward compat. Otherwise: literal ID.
            try:
                n = int(n_str)
            except ValueError:
                print(c(f"  not a number: {n_str!r}", RED))
                return None
            lst = getattr(state, "last_mem_list", []) or []
            # If n matches an ID in last_mem_list, prefer that (unambiguous).
            if n in lst:
                return n
            # If n is a small position and no collision, allow position lookup.
            if 1 <= n <= len(lst):
                return lst[n - 1]
            # Otherwise treat as raw memory ID — let downstream handler fail
            # loudly if it does not exist (mem.show / forget / etc).
            return n

        # /mem list -> resurfaced memories (also populates last_mem_list)
        if args and args[0].lower() == "list":
            resurfaced = list(getattr(state, "resurfaced_memories", []))[-20:]
            print_resurfaced_memories(resurfaced, limit=20)
            state.last_mem_list = [m.get("id") for m in resurfaced]
            return True

        # /mem search [query...]
        if args and args[0].lower() == "search":
            query = " ".join(args[1:]).strip()
            if not query:
                # delegate to wizard for an inline prompt + display
                launch_wizard("mem.search", runner)
                return True
            try:
                rows = mem.recall_content(query, top_k=10)
            except Exception as ex:
                print(c(f"  search failed: {type(ex).__name__}: {ex}", RED))
                return True
            _show_numbered(rows, f"top {min(10, len(rows))} for {query!r}")
            return True

        # /mem show <N> -> full memory dump (content, tags, triggers, history)
        if args and args[0].lower() == "show":
            if len(args) < 2:
                print(c("  usage: /mem show <N>   (N from last /mem list or /mem search)", RED))
                return True
            mid = _resolve_n(args[1])
            if mid is None:
                return True
            try:
                data = mem.show(int(mid))
            except Exception as ex:
                print(c(f"  show failed: {type(ex).__name__}: {ex}", RED))
                return True
            import json as _json
            print(c(_json.dumps(data, indent=2, ensure_ascii=False), DIM))
            return True

        # /mem <verb> <N>  INSTANT single-target ops (no wizard, no confirm).
        _verb_to_api = {
            "forget":    lambda _mem, _mid: _mem.forget(_mid),
            "deprecate": lambda _mem, _mid: _mem.deprecate(_mid, reason="/mem"),
            "pin":       lambda _mem, _mid: _mem.pin(_mid),
            "unpin":     lambda _mem, _mid: _mem.unpin(_mid),
        }
        if args and args[0].lower() in _verb_to_api and len(args) >= 2:
            verb = args[0].lower()
            # Accept comma-separated batch: /mem forget 12,15, 27 -> all three.
            tokens = [tok for tok in " ".join(args[1:]).replace(",", " ").split() if tok]
            ids: list[int] = []
            bad: list[str] = []
            for tok in tokens:
                if not tok.lstrip("-").isdigit():
                    bad.append(tok); continue
                resolved = _resolve_n(tok)
                if resolved is None:
                    bad.append(tok); continue
                ids.append(int(resolved))
            if bad:
                print(c(f"  ignored non-numeric: {', '.join(bad)}", RED))
            if not ids:
                return True
            ok, fail = 0, 0
            for mid in ids:
                try:
                    _verb_to_api[verb](mem, mid)
                    print(c(f"  {verb} #{mid} ok", GREEN))
                    ok += 1
                except Exception as ex:
                    print(c(f"  {verb} #{mid} failed: {type(ex).__name__}: {ex}", RED))
                    fail += 1
            if len(ids) > 1:
                print(c(f"  -- {verb}: {ok} ok, {fail} failed --", DIM))
            return True

        # /mem triggers <N>  -> list triggers, numbered as <N>.1, <N>.2, ...
        if args and args[0].lower() == "triggers":
            if len(args) < 2:
                print(c("  usage: /mem triggers <N>", RED)); return True
            mid = _resolve_n(args[1])
            if mid is None:
                return True
            try:
                data = mem.trigger_list(int(mid))
                trigs = data.get("triggers", []) if isinstance(data, dict) else (data or [])
            except Exception as ex:
                print(c(f"  triggers failed: {type(ex).__name__}: {ex}", RED)); return True
            if not trigs:
                print(c(f"  memory #{mid} has no triggers", DIM)); return True
            print(c(f"  triggers for memory #{mid} ({len(trigs)}):", DIM))
            for i, t in enumerate(trigs, start=1):
                txt = (t.get("text") or t.get("trigger") or "").replace(chr(10), " ")
                print(f"    {mid}.{i}  {txt}")
            return True

        # /mem trigger remove <N.M>   /mem trigger add <N> [a; b; c]
        if args and args[0].lower() == "trigger":
            if len(args) < 2:
                print(c("  usage: /mem trigger add <N> [a; b; c]  |  /mem trigger remove <N.M>", RED))
                return True
            sub = args[1].lower()
            if sub == "remove":
                if len(args) < 3:
                    print(c("  usage: /mem trigger remove <N.M>", RED)); return True
                ref = args[2]
                if "." not in ref:
                    print(c("  ref must be <memory>.<position>, e.g. 10.3", RED)); return True
                mpart, _, ipart = ref.partition(".")
                try:
                    mid = int(mpart); idx = int(ipart)
                except ValueError:
                    print(c(f"  bad ref: {ref!r}", RED)); return True
                try:
                    data = mem.trigger_list(mid)
                    trigs = data.get("triggers", []) if isinstance(data, dict) else (data or [])
                except Exception as ex:
                    print(c(f"  lookup failed: {type(ex).__name__}: {ex}", RED)); return True
                if not (1 <= idx <= len(trigs)):
                    print(c(f"  no trigger {mid}.{idx} (only {len(trigs)} triggers)", RED)); return True
                tid = trigs[idx - 1].get("id")
                try:
                    mem.trigger_remove(tid)
                    print(c(f"  removed trigger {mid}.{idx} (id={tid})", GREEN))
                except Exception as ex:
                    print(c(f"  remove failed: {type(ex).__name__}: {ex}", RED))
                return True
            if sub == "add":
                if len(args) < 3:
                    print(c("  usage: /mem trigger add <N> [a; b; c]", RED)); return True
                try:
                    mid_arg = int(args[2])
                except ValueError:
                    print(c(f"  bad memory id: {args[2]!r}", RED)); return True
                # Re-extract bracketed payload from original raw_after_cmd because shlex
                # may have stripped/split brackets. We expect cmd_args_raw available.
                raw = (cmd_args_raw if "cmd_args_raw" in dir() else " ".join(args[3:])).strip()
                m_open = raw.find("[")
                m_close = raw.rfind("]")
                if m_open != -1 and m_close > m_open:
                    payload = raw[m_open + 1 : m_close]
                else:
                    payload = " ".join(args[3:]).strip()
                if not payload.strip():
                    print(c("  no triggers given (wrap in [ ] separated by ;)", RED)); return True
                items = [t.strip() for t in payload.split(";") if t.strip()]
                if not items:
                    print(c("  no non-empty triggers parsed", RED)); return True
                added = 0; failed = 0
                for t in items:
                    try:
                        mem.trigger_add(mid_arg, t)
                        added += 1
                    except Exception as ex:
                        failed += 1
                        print(c(f"  add failed for {t!r}: {type(ex).__name__}: {ex}", RED))
                print(c(f"  added {added} trigger(s) to #{mid_arg}" + (f"  ({failed} failed)" if failed else ""), GREEN if added else RED))
                return True
            print(c(f"  unknown /mem trigger subcommand: {sub}", RED)); return True

        # /mem edit <N>
        if args and args[0].lower() == "edit":
            if len(args) < 2:
                print(c("  usage: /mem edit <N>   (N from last /mem list or /mem search)", RED))
                return True
            mid = _resolve_n(args[1])
            if mid is None:
                return True
            # CRUD-on-this-memory action picker
            actions = [
                ("rewrite content (supersede)", "mem.update_content"),
                ("add trigger",                  "mem.trigger.add"),
                ("remove trigger",               "mem.trigger.remove"),
                ("list triggers",                "mem.trigger.list"),
                ("pin (always-inject)",          "mem.pin"),
                ("unpin",                        "mem.unpin"),
                ("deprecate (soft delete)",      "mem.deprecate"),
                ("forget (PERMANENT)",           "mem.forget"),
            ]
            picker = Wizard(
                name="__mem_edit_picker__", title=f"edit memory #{mid}",
                steps=[Step(key="action", kind="pick", prompt="what to change?",
                            choices=lambda c: [Choice(value=w, label=lbl) for lbl, w in actions])],
            )
            try:
                r = picker.run(runner)
                wname = r.get("action") if isinstance(r, dict) else r
            except Cancelled:
                return True
            if wname:
                launch_wizard(wname, runner, initial={"memory_id": int(mid)})
            return True

        # /mem with no args -> CRUD menu (action -> sub-wizard)
        if not args:
            menu = [
                ("[R] list   last 20 resurfaced memories", "__list__"),
                ("[R] search semantic top-20 by query",     "mem.search"),
                ("[C] remember new fact",                   "mem.remember"),
                ("[U] edit a memory (pick + change)",       "__edit__"),
                ("[D] deprecate (soft delete)",             "mem.deprecate"),
                ("[D] forget (PERMANENT)",                  "mem.forget"),
                ("more... (all mem.* wizards)",             "__all__"),
            ]
            picker = Wizard(
                name="__mem_crud__", title="memory CRUD",
                steps=[Step(key="pick", kind="pick", prompt="pick an action",
                            choices=lambda c: [Choice(value=v, label=l) for l, v in menu])],
            )
            try:
                r = picker.run(runner)
                pick = r.get("pick") if isinstance(r, dict) else r
            except Cancelled:
                return True
            if pick == "__list__":
                resurfaced = list(getattr(state, "resurfaced_memories", []))[-20:]
                print_resurfaced_memories(resurfaced, limit=20)
                state.last_mem_list = [m.get("id") for m in resurfaced]
            elif pick == "__edit__":
                # pick the memory via the standard chooser, then route
                try:
                    data = mem.list_memories(limit=100)
                    rows = data.get("memories", []) if isinstance(data, dict) else data
                except Exception as ex:
                    print(c(f"  list failed: {type(ex).__name__}: {ex}", RED))
                    return True
                edit_picker = Wizard(
                    name="__mem_edit_pick__", title="pick memory to edit",
                    steps=[Step(key="memory_id", kind="pick", prompt="pick a memory",
                                choices=lambda c: [Choice(value=r.get("id"),
                                                          label=f"#{r.get('id')}  {(r.get('content') or '')[:80]}",
                                                          preview=(r.get('content') or '')[:600])
                                                   for r in rows])],
                )
                try:
                    r2 = edit_picker.run(runner)
                    mid = r2.get("memory_id") if isinstance(r2, dict) else r2
                except Cancelled:
                    return True
                if mid is not None:
                    # Re-use the edit-action picker inline:
                    actions = [
                        ("rewrite content (supersede)", "mem.update_content"),
                        ("add trigger",                  "mem.trigger.add"),
                        ("remove trigger",               "mem.trigger.remove"),
                        ("list triggers",                "mem.trigger.list"),
                        ("pin (always-inject)",          "mem.pin"),
                        ("unpin",                        "mem.unpin"),
                        ("deprecate (soft delete)",      "mem.deprecate"),
                        ("forget (PERMANENT)",           "mem.forget"),
                    ]
                    act_picker = Wizard(
                        name="__mem_edit_act__", title=f"edit memory #{mid}",
                        steps=[Step(key="action", kind="pick", prompt="what to change?",
                                    choices=lambda c: [Choice(value=w, label=lbl) for lbl, w in actions])],
                    )
                    try:
                        r3 = act_picker.run(runner)
                        wname = r3.get("action") if isinstance(r3, dict) else r3
                    except Cancelled:
                        return True
                    if wname:
                        launch_wizard(wname, runner, initial={"memory_id": int(mid)})
            elif pick == "__all__":
                all_picker = Wizard(
                    name="__mem_all__", title="all memory wizards",
                    steps=[Step(key="name", kind="pick", prompt="pick a wizard",
                                choices=lambda c: [Choice(value=n, label=n) for n in names])],
                )
                try:
                    r4 = all_picker.run(runner)
                    nm = r4.get("name") if isinstance(r4, dict) else r4
                    if nm:
                        launch_wizard(nm, runner)
                except Cancelled:
                    pass
            elif pick:
                launch_wizard(pick, runner)
            return True

        # /mem <dotted-suffix>  -> direct wizard
        sub = "mem." + ".".join(args)
        if sub in names:
            launch_wizard(sub, runner)
        else:
            candidates = [n for n in names if n.startswith(sub)]
            if len(candidates) == 1:
                launch_wizard(candidates[0], runner)
            elif not candidates:
                print(c(f"no wizard {sub!r}. known: {names}", RED))
            else:
                print(c(f"ambiguous: {candidates}", YELLOW))
        return True

    if cmd == "compact":
        keep = int(args[0]) if args and args[0].isdigit() else 6
        before = len(state.history)
        if before <= keep:
            print(c(f"  history has {before} turns, keep {keep} — nothing to do", DIM))
            return True
        state.history = state.history[-keep:]
        print(c(f"  compacted: {before} -> {len(state.history)} turns kept", GREEN))
        return True

    if cmd == "sessions":
        if not args:
            print(c("  usage: /sessions <query>", RED))
            return True
        sl = sessionlog.get_default()
        if sl is None:
            print(c("  session log not initialized", RED))
            return True
        hits = sl.search(" ".join(args), limit=15)
        if not hits:
            print(c("  no matches", DIM))
            return True
        for h in hits:
            preview = h["content"].replace("\n", " ")[:120]
            sid = h["session_id"][:8]
            role = h["role"]
            print(c(f"  [{sid}/{role}] {preview}", DIM))
        return True

    if cmd == "wiz.delete":
        if not args:
            print(c("  usage: /wiz.delete <name>", RED))
            return True
        ws = wizard_store.get_default()
        ok = ws.delete(args[0]) if ws else False
        print(c(f"  deleted {args[0]}" if ok else f"  no such wizard {args[0]!r}",
                GREEN if ok else RED))
        return True

    if cmd == "wiz.export":
        if not args:
            print(c("  usage: /wiz.export <name>", RED))
            return True
        import json as _json
        from .wizard import REGISTRY as _REG
        from . import wizard_store as _ws_mod
        w = _REG.get(args[0])
        if w is None:
            print(c(f"  no such wizard {args[0]!r}", RED))
            return True
        try:
            print(_json.dumps(_ws_mod.wizard_to_dict(w), indent=2))
        except Exception as ex:
            print(c(f"  {type(ex).__name__}: {ex}", RED))
        return True

    if cmd == "view":
        if not args:
            print(c("  usage: /view <ref>  (e.g. archive:sess/000003 or sess/000003)", RED))
            return True
        arch = toolarchive.get_default()
        if arch is None:
            print(c("  archive not initialized", RED))
            return True
        try:
            body = arch.read(args[0])
            print(body)
        except Exception as ex:
            print(c(f"  {type(ex).__name__}: {ex}", RED))
        return True

    if cmd == "trace":
        tw = tracelog.get_default()
        if tw is None:
            print(c("  trace writer not initialized", RED))
            return True
        n = int(args[0]) if args and args[0].isdigit() else 20
        rows = tw.recent(n)
        if not rows:
            print(c("  (no traces yet)", DIM))
            return True
        for r in rows:
            dt = (r.get("ended_at") or 0) - r.get("started_at", 0)
            print(c(f"  {r['id']}  {r['wizard']:30s}  {r['source']:5s}  {r['status']:9s}  {dt:.2f}s  parent={r['parent_id'] or '-'}", DIM))
        return True

    if cmd == "stats":
        from .stats import report as _stats_report
        print(_stats_report())
        return True

    if cmd == "replay":
        if not args:
            print(c("  usage: /replay <trace_id>", RED))
            return True
        tw = tracelog.get_default()
        if tw is None:
            print(c("  trace writer not initialized", RED))
            return True
        row = tw.get(args[0])
        if row is None:
            print(c(f"  no trace {args[0]!r}", RED))
            return True
        import json as _json
        try:
            initial = _json.loads(row["ctx_json"])
        except Exception:
            initial = {}
        # strip framework keys so user can re-walk fresh
        for k in ("__wizard__", "__trace_id__", "__source__"):
            initial.pop(k, None)
        launch_wizard(row["wizard"], runner)  # ctx defaults wiring is per-step (future polish)
        return True

    if cmd == "warm":
        sp = Spinner(f"loading {cfg.model}").start()
        try:
            r = preload(cfg.ollama_url, cfg.model, keep_alive="24h")
        except Exception as e:
            sp.stop("load FAILED")
            print(c(f"  {type(e).__name__}: {e}", RED))
            return True
        sp.stop(f"loaded {cfg.model} (pinned 24h)")
        return True
    if cmd == "cold":
        try:
            unload(cfg.ollama_url, cfg.model)
            print(c(f"  evicted {cfg.model} from VRAM", DIM))
        except Exception as e:
            print(c(f"  unload FAILED: {e}", RED))
        return True
    if cmd == "stats":
        st = getattr(llm_for_stats, "last_stats", {})
        if not st:
            print(c("  no stats yet — run a turn first", DIM))
        else:
            pe = st.get("prompt_eval_count", 0); pms = st.get("prompt_eval_ms", 0) or 1
            ec = st.get("eval_count", 0); ems = st.get("eval_ms", 0) or 1
            print(c(f"  load={st.get('load_ms',0)}ms  prompt={pe}tok/{pms}ms ({pe*1000//pms}t/s)  gen={ec}tok/{ems}ms ({ec*1000//ems}t/s)  total={st.get('total_ms',0)}ms", DIM))
        return True

    print(c(f"unknown command: /{cmd}", RED))
    return True


def make_input_session(cfg: Config) -> PromptSession:
    hist_path = cfg.state_dir / "history"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    slash_words = ["/help", "/quit", "/exit", "/model", "/ctx", "/sys",
                   "/history", "/reset", "/warm", "/cold", "/stats", "/wiz", "/mem", "/trace", "/replay", "/view", "/wiz.delete", "/wiz.export", "/sessions", "/compact", "/project", "/proj"]
    wiz_words = ["/wiz " + n for n in list_wizards()]
    mem_words = ["/mem " + n.removeprefix("mem.").replace(".", " ")
                 for n in list_wizards("mem.")]
    completer = WordCompleter(slash_words + wiz_words + mem_words, ignore_case=True, sentence=True)
    return PromptSession(history=FileHistory(str(hist_path)), completer=completer)


def main():
    cfg = Config()
    cfg.ensure_dirs()
    import uuid as _uuid
    session_id = "swisz_" + _uuid.uuid4().hex[:8]
    pools.set_default_db(cfg.state_dir / "pools.db")
    tracelog.set_default(tracelog.TraceWriter(cfg.state_dir / "traces.db"))
    toolarchive.set_default(toolarchive.ToolArchive(cfg.state_dir / "archive", session_id))
    _ws = wizard_store.WizardStore(cfg.state_dir / "wizards.db")
    wizard_store.set_default(_ws)
    wizards_meta.register_meta_wizards()
    _n_loaded = _ws.load_into_registry()
    if _n_loaded:
        print(c(f"  loaded {_n_loaded} persisted wizard(s)", DIM))
    sessionlog.set_default(sessionlog.SessionLog(cfg.state_dir / "sessions.db"))

    try:
        swiszard_do = load_swiszard_do(cfg.swiszard_path)
    except SwiszardUnavailable as e:
        print(c(f"FATAL: {e}", RED))
        print(c("set SWISZCLI_SWISZARD_PATH to your swiszard repo root", DIM))
        return 2

    # ---- SWISZARD CALL LOG (every swiszard_do invocation, per session) --
    _swisz_call_log = swiszcalls.SwiszCallLog(cfg.state_dir, session_id)
    swiszcalls.set_default(_swisz_call_log)
    swiszard_do = swiszcalls.wrap_swiszard_do(swiszard_do, _swisz_call_log)

    
    # ---- PROJECT DISPATCH WRAP ----------------------------------------
    _orig_swiszard_do_for_proj = swiszard_do
    def _proj_aware_swiszard_do(task):
        try:
            result = try_project_dispatch(task, cfg.mem_url)
        except Exception:
            result = None
        if result is not None:
            return result
        return _orig_swiszard_do_for_proj(task)
    swiszard_do = _proj_aware_swiszard_do

    # ---- MEMORY DISPATCH WRAP (outermost) ------------------------------
    # Intercept memory <verb> tasks (and strip XML wrappers) BEFORE the
    # upstream router gets a chance to TF-IDF them into a recall query.
    _orig_swiszard_do_for_mem = swiszard_do
    def _mem_aware_swiszard_do(task):
        try:
            result = try_memory_dispatch(task, mem)
        except Exception:
            result = None
        if result is not None:
            return result
        cleaned = strip_xml_wrapper(task) if task else task
        return _orig_swiszard_do_for_mem(cleaned)
    swiszard_do = _mem_aware_swiszard_do


    mem = MemoryClient(cfg.mem_url, session_id=session_id)
    try:
        mem.health()
    except Exception as e:
        print(c(f"WARN: swizmem unreachable at {cfg.mem_url}: {e}", YELLOW))

    # Register all wizards
    wizards_mem.register_all(mem)
    _pc = ProjectClient(cfg.mem_url)
    wizards_proj.register_all(_pc)
    runner = PTKRunner()

    llm = make_llm(cfg)
    state = AgentState(
        system_prompt=build_system_prompt_full(session_id),
        ctx_turns=cfg.ctx_turns,
    )
    state.resurfaced_memories = []

    state.last_code_hits = []

    def recall_fn(query):
        mems = mem.recall_triggers(query, top_k=5)
        seen = {m.get("id") for m in getattr(state, "resurfaced_memories", [])}
        for m in mems:
            mid = m.get("id")
            if mid is None or mid in seen:
                continue
            state.resurfaced_memories.append(m)
            seen.add(mid)
        if len(state.resurfaced_memories) > 20:
            state.resurfaced_memories = state.resurfaced_memories[-20:]
        render_recall_banner(mems)
        # Code-context injection: if any roots are indexed, fetch top chunks.
        state.last_code_hits = []
        try:
            roots = mem.code_index_list().get("roots", [])
            active = [r for r in roots if r.get("active") and r.get("chunks")]
            if active:
                resp = mem.code_search(query, top_k=5)
                hits = [h for h in resp.get("hits", []) if h.get("score", 0) >= 0.35]
                state.last_code_hits = hits
                if hits:
                    paths = sorted({h["path"].rsplit("/", 1)[-1] for h in hits})[:4]
                    print(c(f"  code▸ {len(hits)} chunks from {', '.join(paths)}", DIM))
        except Exception as e:
            print(c(f"  code▸ search failed: {e}", DIM))
        return mems

    def combined_renderer(mems):
        parts = []
        m = build_memory_block(mems)
        if m:
            parts.append(m)
        c_hits = getattr(state, "last_code_hits", [])
        if c_hits:
            parts.append(build_code_context_block(c_hits))
        return "\n\n".join(parts)

    def on_tool_start(task):
        print()
        print(c(f"  swisz▸ {task[:160]}", MAGENTA), flush=True)

    def on_tool_end(task, result, dt):
        try:
            _p0_capture.record_tool_result(task, result)
        except Exception:
            pass
        # sequence-learn: append this call to current turn buffer
        try:
            _wname = task.strip().split(":", 1)[0].split()[0] if task else "?"
            state._turn_calls.append({"wizard": _wname, "task": (task or "")[:300]})
            try:
                state._fp.record_task(task or "")
            except Exception:
                pass
        except Exception:
            pass
# P1 observational learning removed — router feedback loop now handles this (routes.db)
        preview = result.replace("\n", " ")[:200]
        suffix = "..." if len(result) > 200 else ""
        ok = "ok" if not result.startswith("ERROR") else "ERR"
        col = GREEN if ok == "ok" else RED
        print(c(f"  {ok} {dt:.2f}s  {preview}{suffix}", col if ok == "ERR" else DIM))

    def on_token(tok):
        sys.stdout.write(tok)
        sys.stdout.flush()

    # Safety gate confirm handler (phase 7d).
    # mode "off" => lambda returning True (allow all). "block" => None.
    # "confirm" => interactive y/N prompt at TTY.
    if cfg.safety_mode == 'off':
        confirm_destructive = lambda task, v: True
    elif cfg.safety_mode == 'block':
        confirm_destructive = None
    else:
        def confirm_destructive(task, v):
            print()
            print(c(f'  ⚠  DESTRUCTIVE: {v.summary()}', YELLOW))
            print(c(f'     task: {task[:200]}', YELLOW))
            try:
                ans = input(c('     proceed? [y/N]: ', YELLOW)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = 'n'
            return ans == 'y'

    # ---- P0 swiszContext wiring (2026-06-01) -------------------------------
    _p0_store = _CtxStore(db_path=cfg.state_dir / "contexts.db")
    _p0_router = _Router(_p0_store)
    try:
        _p0_seeded = _p0_router.seed()
        if _p0_seeded:
            print(c(f"  P0: seeded {_p0_seeded} router examples", DIM))
        print(c(f"  P0: contexts.db at {_p0_store.db_path} ({_p0_store.count_examples()} examples)", DIM))
    except Exception as _p0_e:
        print(c(f"  P0: router seed failed (continuing without hints): {_p0_e}", YELLOW))
    _p0_capture = _ChunkCapture(store=_p0_store, session_id=session_id, window_size=8)
    state.last_user_input = ""
    state.last_p0_router_hint = ""
    state.last_p0_chunks_block = ""

    _orig_recall_fn = recall_fn
    def recall_fn(query):
        _stats_incr("turns")
        # flush any sequence accumulated during the previous turn BEFORE updating user_text
        try:
            if getattr(state, "_flush_sequence", None):
                state._flush_sequence()
        except Exception:
            pass
        state.last_user_input = query
        mems = _orig_recall_fn(query)
        # also recall chunks from swiszContext
        try:
            _qvec = _embed(query) if query and query.strip() else None
            # P1.15: blend task fingerprint into query vector
            try:
                _fp = getattr(state, "_fp", None)
                if _qvec is not None and _fp is not None and not _fp.is_empty():
                    _fp_text = _fp.render()
                    if _fp_text.strip():
                        _fp_vec = _embed(_fp_text)
                        _qvec = _fp_blend(_qvec, _fp_vec)
            except Exception:
                pass
            if _qvec is not None:
                hits = _p0_store.recall_chunks(_qvec, top_k=5, session_id=session_id, min_score=0.55)
                state.last_p0_chunks_block = _render_chunks(hits)
                if hits:
                    _top = hits[0]["score"]
                    print(c(f"  ctx▸ {len(hits)} swiszContext chunks (top score {_top:.2f})", DIM))
            else:
                state.last_p0_chunks_block = ""
        except _EmbedError as _e:
            state.last_p0_chunks_block = ""
            print(c(f"  ctx▸ embed failed: {_e}", YELLOW))
        except Exception as _e:
            state.last_p0_chunks_block = ""
            print(c(f"  ctx▸ recall failed: {_e}", YELLOW))
        # also compute a router hint for this turn
        try:
            _decision = _p0_router.decide(query)
            state.last_p0_router_hint = _router_hint(_decision)
            if _decision.mode != "fallback":
                print(c(f"  hint▸ {_decision.mode}: wizard={_decision.wizard_name} score={_decision.score:.2f}", DIM))
        except Exception as _e:
            state.last_p0_router_hint = ""
            print(c(f"  hint▸ router failed: {_e}", YELLOW))
        return mems

    _orig_renderer = combined_renderer
    def combined_renderer(mems):
        base = _orig_renderer(mems) or ""
        extras = []
        rh = getattr(state, "last_p0_router_hint", "")
        if rh:
            extras.append(rh)
        cb = getattr(state, "last_p0_chunks_block", "")
        if cb:
            extras.append(cb)
        if not extras:
            return base
        if base:
            return base + chr(10) + chr(10) + chr(10).join(extras)
        return chr(10).join(extras)
    # ---- end P0 wiring -----------------------------------------------------
    # ---- Scratchpad reasoning (jarvis option B, 2026-06-01) ----------------
    _sp_store = _SPStore(db_path=cfg.state_dir / "scratchpad.db")
    _sp_ops = _SPOps(_sp_store, session_id)
    try:
        _sp_existing = _sp_store.get_active(session_id)
        if _sp_existing and not _sp_existing.is_done:
            print(c(f"  scratchpad: resumed active plan ({len(_sp_existing.plan)} steps, on step {_sp_existing.cursor + 1})", DIM))
        else:
            print(c(f"  scratchpad: ready ({_sp_store.db_path})", DIM))
    except Exception as _spe:
        print(c(f"  scratchpad: init warn: {_spe}", YELLOW))

    _orig_swiszard_do_for_sp = swiszard_do
    def swiszard_do(task):
        try:
            _handled, _out = _sp_dispatch(task, _sp_ops)
            if _handled:
                return _out
        except Exception as _e:
            return f"scratchpad dispatch error: {_e}"
        return _orig_swiszard_do_for_sp(task)

    _renderer_before_scratchpad = combined_renderer
    def combined_renderer(mems):
        base = _renderer_before_scratchpad(mems) or ""
        try:
            _sp_active = _sp_store.get_active(session_id)
        except Exception:
            _sp_active = None
        if _sp_active and not _sp_active.is_done:
            block = _sp_active.render()
            if base:
                return base + chr(10) + chr(10) + block
            return block
        return base

    _scratchpad_doc = chr(10).join([
        "",
        "## scratchpad (external structured memory for multi-step tasks)",
        "Use these swiszard calls to plan, track, and reason across many steps.",
        "Your scratchpad is auto-injected into every turn prompt while active.",
        "",
        "  plan: GOAL | step1 | step2 | step3        create a plan",
        "  observe: action ## result                  log what happened",
        "  done                                       mark current step complete",
        "  done: result summary                       same + summary",
        "  decide: choice ## why                      record a decision",
        "  blocker: text                              note a blocker",
        "  insert: text                               insert new step after current",
        "  scratchpad                                 show current scratchpad",
        "  abandon: reason                            abandon current plan",
        "",
        "RULE: any task that requires more than 2 tool calls MUST start with plan:",
        "RULE: after every tool call, emit observe: or done: to update the scratchpad",
        "RULE: if blocked, emit blocker: and revise with insert: or abandon:",
        "RULE: the scratchpad is your working memory. Read it. Trust it over your own recall.",
    ])
    state.system_prompt = state.system_prompt + chr(10) + _scratchpad_doc
    _seq_hint_doc = chr(10).join([
        "",
        "## sequence hints (multi-step recipes you have learned)",
        "If a <sequence_hint> block appears in the prompt, it means past similar inputs",
        "led to a specific multi-step pipeline. Emit those swiszard calls in order",
        "without asking the user mid-sequence. Only deviate if the current context",
        "clearly demands it (note the deviation in scratchpad with decide:).",
    ])
    state.system_prompt = state.system_prompt + chr(10) + _seq_hint_doc
    _edit_doc = chr(10).join([
        "",
        "## structural editing (AST-aware, with full undo)",
        "All edits MUST go through these wizards. NEVER use sed or raw write_b64",
        "to modify code files -- they bypass undo history.",
        "",
        "  find symbol NAME                          locate a function/class in the project",
        "  find symbols in /path/file.py             list all symbols in a file",
        "  index project [/path]                     re-index AST symbols (auto on boot)",
        "",
        "  edit replace /path \"OLD\" with \"NEW\" -- description",
        "                                            propose substring replacement (must be UNIQUE)",
        "  edit func /path SYMBOL with:              propose AST function/class body replacement",
        "  <NEW_SOURCE_HERE>                         (source spans following lines)",
        "  edit apply                                APPLY the staged proposal",
        "  edit cancel                               DROP the staged proposal",
        "",
        "  edit history [/path]                      list recent edits",
        "  edit undo [ID]                            revert last edit (or specific id)",
        "",
        "RULE: every edit shows a unified diff preview BEFORE applying.",
        "RULE: read the diff. If it looks wrong, emit \"edit cancel\" and try again.",
        "RULE: anything that gets applied is logged with full pre/post snapshots.",
        "RULE: if a change breaks something, emit \"edit undo\" -- never panic-write.",
    ])
    state.system_prompt = state.system_prompt + chr(10) + _edit_doc

    # ---- project state (auto-load per-repo workspace) -------------------
    _proj_store = _ProjStore()
    _proj_state = _proj_store.load()  # uses cwd
    state._proj_state = _proj_state
    state._fp = _TaskFingerprint()
    if _proj_state:
        state._fp.set_project(_proj_state.name)
    state._spec = _SpecCache(ttl_seconds=60.0)
    state._spec_hits = 0
    state._spec_misses = 0
    if _proj_state:
        _proj_block = _proj_state.render()
        state.system_prompt = state.system_prompt + chr(10) + chr(10) + _proj_block
        try:
            print(c("  proj▸ " + _proj_state.name + " (" + str(_proj_state.sessions_count) + " prior sessions)", DIM))
        except Exception:
            pass
    # ---- end project state -----------------------------------------------

    # ---- edit engine + AST index (structural editing with undo) --------
    _edit_engine = _EditEngine()
    _ast_index = _ASTIndex()
    _edit_ops = _EditOps(_edit_engine, _ast_index, session_id=session_id, project_id=(_proj_state.id if _proj_state else ""))
    if _proj_state:
        try:
            _idx_res = _ast_index.index_project(_proj_state.root, project_id=_proj_state.id)
            print(c("  ast▸ indexed " + str(_idx_res["indexed"]) + " new, " + str(_idx_res["unchanged"]) + " unchanged, " + str(_idx_res["errors"]) + " errors", DIM))
            _ast_stats = _ast_index.stats(project_id=_proj_state.id)
            print(c("  ast▸ " + str(_ast_stats["symbols"]) + " symbols across " + str(_ast_stats["files"]) + " files", DIM))
        except Exception as _idx_e:
            print(c("  ast▸ indexing failed: " + str(_idx_e), YELLOW))
    # ---- end edit engine ------------------------------------------------
    _swd_before_edit = swiszard_do
    def swiszard_do(task):
        # Edit/AST DSL: intercept before any other dispatch
        try:
            if _edit_dsl_match(task):
                _r = _edit_dispatch(_edit_ops, task)
                if _r is not None:
                    return _r
        except Exception as _ee:
            return "edit dispatcher error: " + str(_ee)
        return _swd_before_edit(task)

    # ---- end scratchpad wiring --------------------------------------------

    # ---- sequence learning (multi-step recipe capture) ------------------
    _seq_store = _SeqStore(_p0_store._conn)
    state._turn_calls = []  # accumulates swiszard calls per assistant turn
    state._last_seq_user_text = ""
    state._traj = _Trajectory(window=8)
    state._traj_predicted = None  # vector of predicted next user-turn point

    _renderer_before_seq = combined_renderer
    def combined_renderer(mems):
        base = _renderer_before_seq(mems) or ""
        # P1.9 + P1.14: predicted-trajectory pre-fetch — only when conversation
        # is EXPLORING (drift above settle threshold). When drilling on a
        # narrow topic, prediction is mostly noise, so skip injection.
        try:
            pred = state._traj_predicted
            drift_ok = (not state._traj.is_settled(threshold=0.05)) and (state._traj.drift_magnitude() > 0)
            if pred is not None and drift_ok:
                hits = _p0_store.recall_chunks(pred, top_k=3, session_id=session_id, min_score=0.55) or []
                if hits:
                    bodies = []
                    for h in hits:
                        body = (h.get("body") or h.get("text") or "").strip()
                        if body:
                            bodies.append(body[:240])
                    if bodies:
                        anticip = "<anticipated_context note=\"trajectory-predicted\">\n- " + "\n- ".join(bodies) + "\n</anticipated_context>"
                        base = (base + chr(10) + chr(10) + anticip) if base else anticip
        except Exception:
            pass
        try:
            ut = getattr(state, "last_user_input", "") or ""
            if ut and ut != state._last_seq_user_text:
                vec = _embed(ut)
                matches = _seq_store.find(vec, top_k=1, min_score=0.78)
                if matches:
                    # P1.16 speculative prefetch: prime cache with safe first step
                    try:
                        top = matches[0]
                        steps = top.steps or []
                        for st in steps[:2]:  # first two safe steps
                            t = (st.get("task") or "").strip()
                            if t and _spec_safe(t):
                                state._spec.prime(t, swiszard_do); _stats_incr("spec_attempts")
                    except Exception:
                        pass
                    hint = _render_seq_hint(matches); _stats_incr("sequence_hits")
                    if base:
                        return base + chr(10) + chr(10) + hint
                    return hint
        except Exception:
            pass
        return base

    def _flush_sequence():
        try:
            calls = list(state._turn_calls)
            ut = getattr(state, "last_user_input", "") or ""
            if ut:
                try:
                    uvec = _embed(ut)
                    state._traj.add(uvec)
                    state._traj_predicted = state._traj.predict_next()
                    # P1.9+P1.16: feed trajectory prediction into speculative cache
                    if state._traj_predicted is not None and not state._traj.is_settled():
                        seq_matches = _seq_store.find(state._traj_predicted, top_k=2, min_score=0.65)
                        for match in seq_matches:
                            for step in (match.steps or [])[:2]:
                                task = step.get('task', '') if isinstance(step, dict) else ''
                                if task and _spec_safe(task):
                                    state._spec.prime(task, swiszard_do)
                except Exception:
                    pass
            if len(calls) >= 2 and ut:
                vec = _embed(ut)
                _seq_store.record(ut, vec, calls, source="observed"); _stats_incr("sequences_learned")
            state._turn_calls = []
            state._last_seq_user_text = ut
        except Exception:
            pass
    state._flush_sequence = _flush_sequence
    # ---- end sequence learning -------------------------------------------

    # ---- P1.16 speculative-cache shim around swiszard_do ----------------
    _swd_before_spec = swiszard_do
    def swiszard_do(task):
        try:
            hit = state._spec.lookup(task) if isinstance(task, str) else None
        except Exception:
            hit = None
        if hit is not None:
            state._spec_hits += 1; _stats_incr("spec_hits")
            print(c(f"  spec\u25b8 cache hit ({len(hit)} chars)", DIM))
            # still need to flow through on_tool_end side-effects (sequence + fingerprint)
            try:
                _wname = task.strip().split(":", 1)[0].split()[0] if task else "?"
                state._turn_calls.append({"wizard": _wname, "task": (task or "")[:300]})
                try: state._fp.record_task(task or "")
                except Exception: pass
            except Exception:
                pass
            return hit
        state._spec_misses += 1; _stats_incr("spec_misses")
        return _swd_before_spec(task)

    # ---- P1.5 gap detector + research wizard ------------------------------
    def _post_stream_check(draft_text):
        try:
            v = _gap_detector.detect(draft_text)
        except Exception as e:
            print(c(f"  gap▸ detect failed: {e}", YELLOW))
            v = None
        # P1.7: density-based void detection on claim phrases
        void_queries = []
        try:
            from .embed import embed as _e2
            phrases = _void_detector.extract_claim_phrases(draft_text, max_phrases=3)
            def _corpus():
                try:
                    rows = _p0_store.recent_chunk_vectors(limit=400, session_id=session_id) or []
                    return [(r.get("id"), r["vec"]) for r in rows if r.get("vec")]
                except Exception:
                    return []
            corpus = _corpus()
            if corpus and phrases:
                for ph in phrases:
                    verdict = _void_detector.detect(ph, embed_fn=_e2, corpus_provider=lambda c=corpus: c)
                    if verdict.has_void:
                        void_queries.append(ph); _stats_incr("voids_detected")
                if void_queries:
                    print(c(f"  void▸ {len(void_queries)} low-density claim(s); fetching", YELLOW))
        except Exception as e:
            print(c(f"  void▸ detect failed: {e}", YELLOW))
        # Merge research queries from both detectors
        gap_queries = (v.research_queries if v and v.has_gap else [])
        all_queries = list(dict.fromkeys(gap_queries + void_queries))[:4]
        if not all_queries:
            return None
        print(c(f"  research▸ firing {len(all_queries)} quer(y/ies)", YELLOW)); _stats_incr("voids_filled")
        try:
            # Build context recall fn for the research wizard
            def _ctx_recall(q):
                from .embed import embed as _e2
                try:
                    vec = _e2(q)
                except Exception:
                    return []
                return _p0_store.recall_chunks(vec, top_k=3, session_id=session_id, min_score=0.5)
            evidence = _research_wizard.research(
                all_queries,
                swiszard_do=swiszard_do,
                mem_recall_triggers=mem.recall_triggers,
                context_recall_fn=_ctx_recall,
                mem_remember=mem.remember,
                session_id=session_id,
            )
        except Exception as e:
            print(c(f"  gap▸ research failed: {e}", YELLOW))
            return None
        if not evidence:
            return None
        hint = _gap_detector.hint_block(v) if (v and v.has_gap) else "<research_context_note>Low corpus density near your claim; using fresh evidence.</research_context_note>"
        retry = hint + chr(10) + chr(10) + evidence
        print(c(f"  gap▸ injecting {len(retry)} chars of evidence, retrying", DIM))
        return retry
    # ---- end P1.5 ----------------------------------------------------------

    agent = Agent(
        state=state,
        chat_stream=lambda msgs: llm.chat_stream(msgs),
        swiszard_do=swiszard_do,
        on_token=on_token,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
        recall_fn=recall_fn,
        memory_renderer=combined_renderer,
        archive=toolarchive.get_default(),
        max_tool_iters=cfg.max_tool_iters,
        confirm_destructive=confirm_destructive,
        post_stream_check=_post_stream_check,
    )

    banner(cfg, session_id)

    # Preload model into VRAM so first turn is warm (24h keep_alive).
    if not is_resident(cfg.ollama_url, cfg.model):
        sp = Spinner(f"loading {cfg.model} (cold)").start()
        try:
            preload(cfg.ollama_url, cfg.model, keep_alive="24h")
            sp.stop(f"loaded {cfg.model} (pinned 24h)")
        except Exception as e:
            sp.stop("load FAILED")
            print(c(f"  WARN: preload failed: {e} — first turn will pay cold-load cost", YELLOW))
    else:
        print(c(f"  {cfg.model} already resident in VRAM", DIM))
    session = make_input_session(cfg)

    try:
        while True:
            try:
                line = session.prompt(ANSI(c("you▸ ", GREEN)))
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line.strip():
                continue
            try:
                handled = handle_slash(line, cfg=cfg, mem=mem, state=state, runner=runner, llm_for_stats=llm)
            except SystemExit:
                raise
            if handled:
                continue
            _sl = sessionlog.get_default()
            if _sl is not None:
                _sl.log(session_id, "user", line)
            # ctx budget check: assembled = system + recent history
            try:
                _assembled = state.system_prompt + "\n".join(
                    str(m) for m in (state.history or [])
                ) + line
                _v = ctx_budget.check(_assembled)
                if _v.hard:
                    print(c(f"  CTX HARD CAP exceeded ({_v.tokens} tok > {ctx_budget.HARD})", RED))
                    print(c("  run /compact or /clear before continuing", RED))
                    continue
                if _v.soft:
                    print(c(f"  ctx {_v.tokens} tok > soft {ctx_budget.SOFT}; consider compact", YELLOW))
            except Exception:
                pass
            print(c("caden▸ ", BOLD + CYAN), end="", flush=True)
            _assistant_buf = []
            _orig_on_token = agent.on_token
            def _capture(tok, _orig=_orig_on_token, _buf=_assistant_buf):
                _buf.append(tok)
                _orig(tok)
            agent.on_token = _capture
            try:
                agent.turn(line)
            except Exception as e:
                print()
                print(c(f"agent error: {type(e).__name__}: {e}", RED))
            finally:
                agent.on_token = _orig_on_token
            # P0: record both turns into swiszContext
            try:
                _p0_capture.record_turn("user", line)
                if _assistant_buf:
                    _p0_capture.record_turn("assistant", "".join(_assistant_buf))
            except Exception as _e:
                print(c(f"  ctx▸ record_turn failed: {_e}", YELLOW))
            if _sl is not None and _assistant_buf:
                _sl.log(session_id, "assistant", "".join(_assistant_buf))
            # P1.12 proof loop: did model use last turn's injected evidence?
            try:
                if state._proof.has_pending() and _assistant_buf:
                    draft_text = "".join(_assistant_buf)[:2000]
                    if draft_text.strip():
                        dvec = _embed(draft_text)
                        results = state._proof.score_against(dvec)
                        if results:
                            summary = " ".join(f"{src.split(':')[-1][:14]}={v}({sim})" for src, sim, v, _ in results)
                            print(c(f"  proof▸ {summary}", DIM))
            except Exception:
                pass
            st = llm.last_stats
            if st:
                ec = st.get('eval_count',0); ems = st.get('eval_ms',0) or 1
                pe = st.get('prompt_eval_count',0); pms = st.get('prompt_eval_ms',0) or 1
                tps = ec*1000//ems if ems else 0
                print()
                print(c(f"  [{pe}+{ec}tok  {tps}t/s  total {st.get('total_ms',0)}ms]", DIM))
            print()
    finally:
        try:
            _p0_capture.close_session()
            # Project state: record this session ended
            try:
                if getattr(state, "_proj_state", None) and _proj_state:
                    _proj_store.record_session_end(_proj_state.id, session_id, turns=len(getattr(state, "history", []) or []), summary=getattr(state, "last_user_input", "")[:200])
            except Exception:
                pass
            print(c(f"  P0: session closed, chunks total: {_p0_store.count_chunks()}", DIM))
        except Exception as _e:
            print(c(f"  P0: close_session failed: {_e}", YELLOW))
        try:
            _swisz_call_log.close()
        except Exception:
            pass
        # Wire chain_credit replay on session end (#275)
        try:
            tw = tracelog.get_default()
            if tw:
                from . import chain_credit
                recent = tw.recent(n=50)
                roots = [t for t in recent if t.get('parent_id') is None and t.get('status') == 'ok']
                if roots:
                    last_root = roots[0]
                    last_result_json = last_root.get('result_json') or '{}'
                    import json as _json
                    try:
                        corrected = _json.loads(last_result_json)
                        corrected_str = str(corrected)
                    except Exception:
                        corrected_str = last_result_json
                    assignments = chain_credit.replay_trace_chain(tw, last_root['id'], corrected_str)
                    report = chain_credit.format_assignment_report(assignments)
                    if report.strip():
                        import sys
                        print(report, file=sys.stderr)
        except Exception:
            pass
        mem.close()


if __name__ == "__main__":
    raise SystemExit(main())