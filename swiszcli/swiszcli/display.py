"""Central Rich display module for swiszcli."""
from __future__ import annotations
import sys
from rich.console import Console
from rich.table import Table
from rich.rule import Rule

console = Console(highlight=False)

def print_turn_header(turn_num: int) -> None:
    console.print(Rule(f"turn {turn_num}", style="dim"))

def print_tool_call(task: str) -> None:
    console.print(f"  [magenta]swisz\u25b8[/magenta] {task[:160]}")

def print_tool_result(ok: str, dt: float, preview: str) -> None:
    suffix = "..." if len(preview) >= 200 else ""
    style = "dim" if ok == "ok" else "bold red"
    console.print(f"  [{style}]{ok} {dt:.2f}s  {preview}{suffix}[/{style}]")

def print_token(tok: str) -> None:
    sys.stdout.write(tok)
    sys.stdout.flush()

def print_mem_banner(mems: list[dict]) -> None:
    for m in (mems or [])[:5]:
        txt = (m.get("text") or m.get("body") or "")[:120]
        pin = "[bold yellow]\u2605[/bold yellow] " if m.get("pinned") else ""
        console.print(f"  [dim]mem\u25b8 {pin}{txt}[/dim]")

def print_pm_banner(nodes: list[dict]) -> None:
    for n in (nodes or [])[:5]:
        title = (n.get("title") or n.get("text") or "")[:100]
        console.print(f"  [dim]pm\u25b8 #{n.get("node_id","?")} {title}[/dim]")

def print_ctx_banner(hits: list[dict]) -> None:
    if not hits:
        return
    top = hits[0].get("score", 0)
    paths = sorted({h["path"].rsplit("/", 1)[-1] for h in hits if h.get("path")})[:4]
    console.print(f"  [dim]ctx\u25b8 {len(hits)} chunks ({chr(44).join(paths)}) top={top:.2f}[/dim]")

def print_session_header(session_id: str, model: str, loaded_wizards: list[str]) -> None:
    console.print(f"  [dim]session {session_id}  model={model}  wizards={len(loaded_wizards)}[/dim]")

def print_status_banner(pm_status: dict, memory_count: int, trace_count: int) -> None:
    proj = pm_status.get("project", "?")
    total = pm_status.get("total", 0)
    done = pm_status.get("done", 0)
    active = pm_status.get("active", 0)
    console.print(f"  [dim]{proj:<12} {total:4d} nodes  {done:4d} done  {active:4d} active[/dim]")
    console.print(f"  [dim]{'memory':<12} {memory_count:4d} facts[/dim]")
    console.print(f"  [dim]{'traces':<12} {trace_count:4d} recorded[/dim]")

def print_wizard_table(wizards: list[dict]) -> None:
    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
    table.add_column("name", style="cyan")
    table.add_column("description", style="dim")
    for w in sorted(wizards, key=lambda x: x.get("name", "")):
        name = w.get("name", "")
        desc = (w.get("description") or w.get("title") or "")[:80]
        table.add_row(name, desc)
    console.print(table)

def print_error(msg: str) -> None:
    console.print(f"[bold red]error:[/bold red] {msg}")
