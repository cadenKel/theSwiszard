# prompt_toolkit runner for wizards. Arrow-key fuzzy picker, text editor,
# checkbox list, confirm prompt. Esc / Ctrl-C raises Cancelled.

from __future__ import annotations

from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.completion import FuzzyWordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.shortcuts import confirm
from prompt_toolkit.styles import Style

from .wizard import Cancelled, Choice, Step, Wizard, WizardRunner

# ANSI color constants (mirrored from cli.py for standalone use)
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
MAGENTA = "\x1b[35m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
RESET = "\x1b[0m"


def c(s: str, color: str) -> str:
    import sys
    if not sys.stdout.isatty():
        return s
    return f"{color}{s}{RESET}"


STYLE = Style.from_dict({
    "title":       "bold cyan",
    "subtitle":    "bold #aaaaaa",
    "prompt":      "bold",
    "hint":        "italic #888888",
    "cursor":      "bold cyan",
    "cursor-line": "bg:#222244 #ffffff",
    "preview":     "#888888",
    "preview-key": "bold #aaaaaa",
    "ok":          "ansigreen",
    "err":         "ansired",
    "dim":         "#666666",
    "filter":      "bold #aaaaff",
    "filter-bar":  "bg:#222233 #aaaaff",
    "checkbox-on": "ansigreen",
    "checkbox-off": "#555555",
    "footer":      "#555555",
    "footer-key":  "bold #777777",
})


class PTKRunner(WizardRunner):
    """Prompt-toolkit driven runner: the human walks through steps with
    arrow keys, tab, enter. Esc aborts. Every step shows wizard title +
    step prompt + (where applicable) live preview pane."""

    def __init__(self) -> None:
        self.session: PromptSession = PromptSession()

    def _header(self, wiz: Wizard, step: Step) -> str:
        return f"  {wiz.title} ▸ {step.prompt}"

    # ── text ──────────────────────────────────────────────────────────────
    def do_text(self, wiz: Wizard, step: Step, ctx: dict) -> str:
        from prompt_toolkit import prompt as ptk_prompt
        print()
        print(c("  ── " + self._header(wiz, step) + " ──", CYAN))
        if step.placeholder:
            print(c(f"  hint: {step.placeholder}", DIM))
        while True:
            try:
                # __default__: allow parent wizard to inject a dynamic default
                # via ctx["__default__<step.key>"] (prefill action preceding the text step).
                _inj = ctx.get(f"__default__{step.key}")
                _eff = _inj if _inj is not None else (step.default or "")
                val = ptk_prompt(
                    "  > ",
                    default=str(_eff),
                    multiline=step.multiline,
                )
            except (KeyboardInterrupt, EOFError):
                raise Cancelled()
            if step.validate:
                err = step.validate(val, ctx)
                if err:
                    print(f"  error: {err}")
                    continue
            return val

    # ── confirm ───────────────────────────────────────────────────────────
    def do_confirm(self, wiz: Wizard, step: Step, ctx: dict) -> bool:
        print()
        print(c("  ── " + self._header(wiz, step) + " ──", CYAN))
        try:
            return confirm(c("  [y/n] ", BOLD))
        except (KeyboardInterrupt, EOFError):
            raise Cancelled()

    # ── action ────────────────────────────────────────────────────────────
    def do_action(self, wiz: Wizard, step: Step, ctx: dict) -> Any:
        if step.action is None:
            raise RuntimeError(f"action step {step.key!r} has no action fn")
        return step.action(ctx)

    # ── nested ────────────────────────────────────────────────────────────
    def do_nested(self, wiz: Wizard, step: Step, ctx: dict) -> Any:
        from .wizard import resolve
        from copy import deepcopy
        if not step.nested_wizard:
            raise RuntimeError(f"nested step {step.key!r} missing nested_wizard")
        sub = resolve(step.nested_wizard)
        # Deepcopy so child cannot accidentally mutate parent ctx.
        # Pass parent trace id so nested run hangs off the parent in trace tree.
        return sub.run(self, initial=deepcopy(ctx),
                       parent_trace_id=ctx.get("__trace_id__"),
                       source=ctx.get("__source__"))

    # ── pick: full-screen arrow-key fuzzy picker w/ preview ──────────────
    def do_pick(self, wiz: Wizard, step: Step, ctx: dict) -> Any:
        if step.choices is None:
            raise RuntimeError(f"pick step {step.key!r} has no choices fn")
        choices = step.choices(ctx)
        if not choices:
            print(f"  (no choices for {step.key})")
            raise Cancelled()
        return self._run_picker(self._header(wiz, step), choices)

    def do_pick_or_new(self, wiz: Wizard, step: Step, ctx: dict) -> Any:
        """Top-N choices from a persistent ChoicePool + sentinel "+ new" row.
        On sentinel, prompt for a new value, append to pool, return it.
        On existing pick, touch() to bump usage."""
        if not step.pool:
            raise RuntimeError(f"pick_or_new step {step.key!r} missing pool name")
        from . import pools
        pool = pools.get_pool(step.pool)
        top = pool.top(step.top_n)
        SENTINEL = "__SWISZ_NEW__"
        choices = [Choice(value=e.value, label=f"{e.label}  (used {e.use_count}x)",
                          preview=f"created_by={e.created_by}") for e in top]
        choices.append(Choice(value=SENTINEL, label="+ new (type one)",
                              preview="add a new option to this pool"))
        picked = self._run_picker(self._header(wiz, step), choices)
        if picked == SENTINEL:
            # Prompt for new value via inline text
            from prompt_toolkit import prompt as ptk_prompt
            print(f"\n  {step.new_prompt}")
            try:
                val = ptk_prompt("  + ").strip()
            except (KeyboardInterrupt, EOFError):
                raise Cancelled()
            if not val:
                raise Cancelled()
            created_by = ctx.get("__source__", "user")
            pool.add(val, label=val, created_by=created_by)
            pool.touch(val)
            return val
        pool.touch(picked)
        return picked

    def do_multi(self, wiz: Wizard, step: Step, ctx: dict) -> list:
        if step.choices is None:
            raise RuntimeError(f"multi step {step.key!r} has no choices fn")
        choices = step.choices(ctx)
        if not choices:
            return []
        return self._run_picker(self._header(wiz, step), choices, multi=True)

    def _run_picker(self, header: str, choices: list[Choice], multi: bool = False) -> Any:
        state = {"idx": 0, "filter": "", "selected": set()}

        def filtered() -> list[int]:
            f = state["filter"].lower()
            return [i for i, c in enumerate(choices) if f in c.label.lower()]

        def render():
            # ═══ Header bar ═════════════════════════════════════════
            lines = [("class:title", "  " + header), ("", "\n")]
            sep = " " + "─" * 76 + "\n"
            lines.append(("class:dim", sep))
            
            # Filter bar
            if state["filter"]:
                fb = "  filter: " + state["filter"] + " \n"
                lines.append(("class:filter-bar", fb))
            else:
                lines.append(("", "\n"))
            
            view = filtered()
            if not view:
                lines.append(("class:err", "  (no matches)\n\n"))
            
            # Choices
            for n, i in enumerate(view):
                c = choices[i]
                is_current = n == state["idx"]
                
                if multi:
                    if i in state["selected"]:
                        mark = ("class:checkbox-on", "● ")
                    else:
                        mark = ("class:checkbox-off", "○ ")
                else:
                    mark = ("", "")
                
                if is_current:
                    cursor = ("class:cursor", "▸ ")
                    row_style = "class:cursor-line"
                else:
                    cursor = ("", "  ")
                    row_style = ""
                
                if multi:
                    lines.append((row_style, " " + cursor[1] + mark[1] + c.label + "\n"))
                else:
                    lines.append((row_style, " " + cursor[1] + c.label + "\n"))
            
            # Preview pane
            lines.append(("", "\n"))
            if view:
                cur_i = view[state["idx"]] if state["idx"] < len(view) else view[0]
                c = choices[cur_i]
                if c.preview:
                    lines.append(("class:preview-key", "  preview ▸ "))
                    lines.append(("class:preview", c.preview))
                    lines.append(("", "\n"))
            
            # Footer
            lines.append(("class:dim", "  ─" * 38 + "\n"))
            if multi:
                footer = "  ↑↓ move   space toggle   tab filter   enter confirm   esc cancel"
            else:
                footer = "  ↑↓ move   tab filter   enter pick   esc cancel"
            lines.append(("class:footer", footer + "\n"))
            
            return lines

        kb = KeyBindings()

        @kb.add("up")
        def _up(event):
            state["idx"] = max(0, state["idx"] - 1)

        @kb.add("down")
        def _down(event):
            view = filtered()
            state["idx"] = min(len(view) - 1, state["idx"] + 1) if view else 0

        @kb.add("tab")
        def _tab(event):
            # toggle filter input mode by reading one char... simpler: prompt
            event.app.exit(result=("__filter__", None))

        @kb.add("space")
        def _space(event):
            if not multi:
                return
            view = filtered()
            if view and state["idx"] < len(view):
                i = view[state["idx"]]
                if i in state["selected"]:
                    state["selected"].discard(i)
                else:
                    state["selected"].add(i)

        @kb.add("enter")
        def _enter(event):
            view = filtered()
            if not view:
                return
            if multi:
                event.app.exit(result=("__done__", [choices[i].value for i in sorted(state["selected"])]))
            else:
                event.app.exit(result=("__done__", choices[view[state["idx"]]].value))

        @kb.add("escape", eager=True)
        @kb.add("c-c")
        def _esc(event):
            event.app.exit(result=("__cancel__", None))

        ctrl = FormattedTextControl(text=render, focusable=True, show_cursor=False)
        layout = Layout(HSplit([Window(content=ctrl, wrap_lines=True)]))

        while True:
            app = Application(layout=layout, key_bindings=kb, style=STYLE, full_screen=False, mouse_support=False)
            kind, value = app.run()
            if kind == "__cancel__":
                raise Cancelled()
            if kind == "__done__":
                return value
            if kind == "__filter__":
                # fall back to a tiny inline prompt for the filter text
                from prompt_toolkit import prompt as ptk_prompt
                try:
                    state["filter"] = ptk_prompt("  filter: ", default=state["filter"])
                except (KeyboardInterrupt, EOFError):
                    state["filter"] = ""
                state["idx"] = 0
                continue
