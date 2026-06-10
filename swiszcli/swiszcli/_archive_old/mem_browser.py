"""Full-screen memory browser TUI for swiszCLI -- /mem command.

Buttery-smooth mass-cleanup UX:
  - top bar = live filter (always active, just type)
  - space = mark/unmark for purge
  - d = delete now (no confirm; undo with u)
  - D = forget PERMANENT (one-shot, undo via re-create from snapshot)
  - P = purge all marked
  - u = undo last delete/purge
  - p = pin/unpin
  - e = edit
  - + = add new memory
  - t = add trigger to current, T/x = delete current trigger
  - tab = focus trigger pane
  - mouse: click row to select; click [del]/[+] buttons; click footer keys
  - / and esc cycle focus to filter input
"""
from __future__ import annotations

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, ConditionalContainer, FloatContainer, Float
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.styles import Style
from prompt_toolkit.filters import Condition
from prompt_toolkit.widgets import TextArea, Frame, Dialog, Button, Label
from prompt_toolkit.application.current import get_app
from prompt_toolkit.mouse_events import MouseEventType


_STYLE = Style.from_dict({
    "title":         "bold reverse",
    "footer":        "bg:#222222 #cccccc",
    "footer.key":    "bg:#222222 bold #ffcc00",
    "filter":        "bg:#003366 #ffffff",
    "filter.label":  "bg:#003366 bold #ffffff",
    "selected":      "reverse bold",
    "marked":        "bg:#553300 #ffff66",
    "marked.sel":    "bg:#aa6600 #ffffff bold",
    "pinned":        "bold #ffaa00",
    "deprecated":    "#666666 italic",
    "trigger":       "#88ccff",
    "trigger.sel":   "reverse #88ccff",
    "pane.title":    "bold underline",
    "help":          "#888888",
    "error":         "bold #ff5555",
    "ok":            "bold #55ff55",
    "button.del":    "bg:#660000 #ffcccc bold",
    "button.add":    "bg:#003300 #ccffcc bold",
    "edit-input":    "bg:#000044 #ffffff",
    "dialog":        "bg:#222222 #ffffff",
    "dialog frame.label": "bg:#222222 bold #ffcc00",
})


def _short(text, n=80):
    if text is None:
        return ""
    t = text.replace("\n", " ").replace("\r", " ")
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


class MemBrowser:
    def __init__(self, mem_client):
        self.mem = mem_client
        self.all_memories = []          # full server set
        self.memories = []              # filtered view
        self.cursor = 0
        self.trigger_cursor = 0
        self.triggers = []
        self.marked = set()             # memory ids marked for purge
        self.undo_stack = []            # list of dicts to re-create or re-pin
        self.message = ""
        self.message_kind = "ok"
        self.show_help = False
        self.focus = "list"             # "list" | "filter" | "triggers" | "input"
        # Filter buffer = always live
        self.filter_buffer = Buffer(multiline=False, on_text_changed=self._on_filter_changed)
        # Modal edit
        self.modal_active = False
        self.modal_title = ""
        self.modal_buffer = Buffer(multiline=True)
        self.modal_callback = None
        self._app = None
        self._reload_all()

    # ---- data ----------------------------------------------------------
    def _reload_all(self):
        try:
            data = self.mem.list_memories(limit=2000) or {}
            rows = data.get("memories", data) if isinstance(data, dict) else data
            self.all_memories = rows or []
        except Exception as e:
            self.all_memories = []
            self._flash("load failed: " + str(e), "error")
        self._apply_filter()

    def _apply_filter(self):
        q = self.filter_buffer.text.strip().lower()
        if not q:
            self.memories = list(self.all_memories)
        else:
            self.memories = [m for m in self.all_memories if q in (m.get("content") or "").lower() or q in str(m.get("id", "")) or any(q in (t or "").lower() for t in (m.get("tags") or []))]
        if self.memories:
            self.cursor = min(self.cursor, len(self.memories) - 1)
        else:
            self.cursor = 0
        self._reload_triggers()

    def _on_filter_changed(self, _buf):
        self._apply_filter()

    def _reload_triggers(self):
        self.trigger_cursor = 0
        cur = self.current()
        if not cur:
            self.triggers = []
            return
        try:
            data = self.mem.trigger_list(cur.get("id"))
            self.triggers = data.get("triggers", []) if isinstance(data, dict) else (data or [])
        except Exception as e:
            self.triggers = []
            self._flash("trigger load failed: " + str(e), "error")

    def current(self):
        if not self.memories:
            return None
        return self.memories[self.cursor]

    # ---- ui helpers ----------------------------------------------------
    def _flash(self, text, kind="ok"):
        self.message = text
        self.message_kind = kind

    def _on_row_click(self, idx):
        def handler(mouse_event):
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return
            self.cursor = idx
            self.focus = "list"
            self._reload_triggers()
        return handler

    def _on_trigger_del(self, tid):
        def handler(mouse_event):
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return
            self._trigger_remove_by_id(tid)
        return handler

    def _on_add_trigger_click(self, _me):
        if _me.event_type == MouseEventType.MOUSE_UP:
            self.act_trigger_add()

    def _on_add_mem_click(self, _me):
        if _me.event_type == MouseEventType.MOUSE_UP:
            self.act_add()

    def _list_text(self):
        if not self.memories:
            if self.filter_buffer.text:
                return [("class:help", "  (no matches for filter)\n")]
            return [("class:help", "  (no memories)\n")]
        out = []
        for i, m in enumerate(self.memories):
            mid = m.get("id", "?")
            pinned = bool(m.get("pinned"))
            deprecated = bool(m.get("deprecated"))
            marked = mid in self.marked
            sel = (i == self.cursor and self.focus == "list")
            tag = "*" if pinned else ("x" if deprecated else " ")
            mark = "[X]" if marked else "[ ]"
            content = _short(m.get("content"), 86)
            line = "  " + mark + " " + tag + " #" + str(mid).rjust(4) + "  " + content + "\n"
            if marked and sel:
                style = "class:marked.sel"
            elif marked:
                style = "class:marked"
            elif sel:
                style = "class:selected"
            elif pinned:
                style = "class:pinned"
            elif deprecated:
                style = "class:deprecated"
            else:
                style = ""
            out.append((style, line, self._on_row_click(i)))
        return out

    def _details_text(self):
        m = self.current()
        if not m:
            return [("class:help", "  (nothing selected)")]
        mid = m.get("id")
        marked = mid in self.marked
        out = [
            ("class:pane.title", "  memory #" + str(mid) + ("   [MARKED FOR PURGE]" if marked else "") + "\n"),
            ("", "\n"),
            ("", "  pinned:     " + str(m.get("pinned")) + "\n"),
            ("", "  deprecated: " + str(m.get("deprecated")) + "\n"),
            ("", "  tags:       " + (", ".join(m.get("tags") or []) or "(none)") + "\n"),
            ("", "  retrievals: " + str(m.get("retrievals", 0)) + "\n"),
            ("", "  source:     " + str(m.get("source", "")) + "\n"),
            ("", "\n"),
            ("class:pane.title", "  content\n"),
            ("", "\n"),
            ("", "  " + (m.get("content") or "").replace("\n", "\n  ") + "\n"),
            ("", "\n"),
            ("class:pane.title", "  triggers  (" + str(len(self.triggers)) + ")  "),
            ("class:button.add", " [+ add trigger] ", self._on_add_trigger_click),
            ("", "\n"),
        ]
        if not self.triggers:
            out.append(("class:help", "  (none -- tab here and press + to add)\n"))
            return out
        for i, t in enumerate(self.triggers):
            tid = t.get("id", "?")
            text = (t.get("text") or t.get("trigger") or "").replace("\n", " ")
            sel = (self.focus == "triggers" and i == self.trigger_cursor)
            row_style = "class:trigger.sel" if sel else "class:trigger"
            out.append((row_style, "  #" + str(tid).rjust(4) + "  " + _short(text, 70) + "  "))
            out.append(("class:button.del", " [x] ", self._on_trigger_del(tid)))
            out.append(("", "\n"))
        return out

    def _filter_prompt(self):
        focused = (self.focus == "filter")
        marker = " ▶ " if focused else "   "
        nmark = (" (" + str(len(self.marked)) + " marked)") if self.marked else ""
        nfound = " [" + str(len(self.memories)) + "/" + str(len(self.all_memories)) + "]"
        return [("class:filter.label", marker + "filter:" + nfound + nmark + " ")]

    def _footer_text(self):
        msg_block = []
        if self.message:
            style = "class:ok" if self.message_kind == "ok" else "class:error"
            msg_block = [(style, "  " + self.message + "\n")]
        else:
            msg_block = [("", "\n")]
        keys = "  ↑↓ nav  space=mark  d=del  D=forget  P=purge marked  u=undo  p=pin  e=edit  +=add  t=trig+  T=trig-  tab=trig pane  /=filter  ?=help  q=quit"
        return msg_block + [("class:footer", keys)]

    def _help_text(self):
        return [
            ("class:pane.title", "  /mem browser -- buttery-smooth purge mode\n"),
            ("", "\n"),
            ("", "  TYPE ANYTHING               live filter (no need to press /)\n"),
            ("", "  ↑ ↓ / j k                   move in memory list\n"),
            ("", "  space                       mark / unmark current for bulk purge\n"),
            ("", "  P                           PURGE all marked (deprecate; undo with u)\n"),
            ("", "  d                           deprecate now (soft delete; undo with u)\n"),
            ("", "  D                           forget PERMANENT (re-add snapshot via u)\n"),
            ("", "  u                           undo last delete/purge\n"),
            ("", "  p                           pin / unpin\n"),
            ("", "  e                           edit content (supersede)\n"),
            ("", "  +                           add new memory\n"),
            ("", "  enter / →                   focus trigger pane\n"),
            ("", "  tab                         cycle focus list ↔ triggers\n"),
            ("", "  t                           add trigger to current memory\n"),
            ("", "  T or x                      delete current trigger\n"),
            ("", "  J / K                       move in triggers pane\n"),
            ("", "  esc                         clear filter (also closes help/modal)\n"),
            ("", "  ?                           toggle help\n"),
            ("", "  q                           quit\n"),
            ("", "\n"),
            ("class:help", "  MOUSE: click a row to select; click [x] to remove a trigger;\n"),
            ("class:help", "         click [+ add trigger] to add one.\n"),
            ("", "\n"),
            ("class:help", "  press ? again to dismiss\n"),
        ]

    # ---- modal input (non-blocking, inline) ----------------------------
    def _open_modal(self, title, default, callback):
        self.modal_active = True
        self.modal_title = title
        self.modal_buffer.text = default or ""
        self.modal_callback = callback
        self.focus = "input"

    def _close_modal(self, commit):
        text = self.modal_buffer.text if commit else None
        cb = self.modal_callback
        self.modal_active = False
        self.modal_callback = None
        self.modal_buffer.text = ""
        self.focus = "list"
        if commit and cb is not None:
            cb(text)

    # ---- actions -------------------------------------------------------
    def act_mark_toggle(self):
        m = self.current()
        if not m: return
        mid = m["id"]
        if mid in self.marked:
            self.marked.discard(mid)
            self._flash("unmarked #" + str(mid))
        else:
            self.marked.add(mid)
            self._flash("marked #" + str(mid) + "  (" + str(len(self.marked)) + " total)")

    def act_purge_marked(self):
        if not self.marked:
            self._flash("nothing marked", "error"); return
        ids = sorted(self.marked)
        snapshots = []
        for mid in ids:
            try:
                m = next((x for x in self.all_memories if x.get("id") == mid), None)
                if m:
                    snapshots.append(dict(m))
                self.mem.deprecate(mid, reason="bulk purge from /mem browser")
            except Exception as e:
                self._flash("purge partial: " + str(e), "error")
        self.undo_stack.append({"op": "deprecate_bulk", "ids": ids, "snapshots": snapshots})
        self.marked.clear()
        self._reload_all()
        self._flash("purged " + str(len(ids)) + " memories (u to undo)")

    def act_deprecate(self):
        m = self.current()
        if not m: return
        mid = m["id"]
        snapshot = dict(m)
        try:
            self.mem.deprecate(mid, reason="from /mem browser")
            self.undo_stack.append({"op": "deprecate", "id": mid, "snapshot": snapshot})
            self.marked.discard(mid)
            self._reload_all()
            self._flash("deprecated #" + str(mid) + " (u to undo)")
        except Exception as e:
            self._flash("deprecate failed: " + str(e), "error")

    def act_forget(self):
        m = self.current()
        if not m: return
        mid = m["id"]
        snapshot = dict(m)
        try:
            self.mem.forget(mid)
            self.undo_stack.append({"op": "forget", "id": mid, "snapshot": snapshot})
            self.marked.discard(mid)
            self._reload_all()
            self._flash("forgot #" + str(mid) + " (u re-creates)")
        except Exception as e:
            self._flash("forget failed: " + str(e), "error")

    def act_undo(self):
        if not self.undo_stack:
            self._flash("nothing to undo", "error"); return
        op = self.undo_stack.pop()
        try:
            if op["op"] == "deprecate":
                self.mem.supersede(op["id"], op["snapshot"].get("content") or "")
                self._flash("restored #" + str(op["id"]))
            elif op["op"] == "deprecate_bulk":
                for snap in op["snapshots"]:
                    try:
                        self.mem.supersede(snap["id"], snap.get("content") or "")
                    except Exception:
                        pass
                self._flash("restored " + str(len(op["snapshots"])) + " memories")
            elif op["op"] == "forget":
                snap = op["snapshot"]
                self.mem.remember(snap.get("content") or "", source="undo:" + str(op["id"]))
                self._flash("re-created from snapshot (new id)")
            elif op["op"] == "pin":
                if op["was_pinned"]:
                    self.mem.pin(op["id"])
                else:
                    self.mem.unpin(op["id"])
                self._flash("undid pin/unpin on #" + str(op["id"]))
        except Exception as e:
            self._flash("undo failed: " + str(e), "error")
        self._reload_all()

    def act_pin(self):
        m = self.current()
        if not m: return
        mid = m["id"]
        was = bool(m.get("pinned"))
        try:
            if was:
                self.mem.unpin(mid); self._flash("unpinned #" + str(mid))
            else:
                self.mem.pin(mid); self._flash("pinned #" + str(mid))
            self.undo_stack.append({"op": "pin", "id": mid, "was_pinned": was})
            self._reload_all()
        except Exception as e:
            self._flash("pin failed: " + str(e), "error")

    def act_edit(self):
        m = self.current()
        if not m: return
        mid = m["id"]
        def commit(text):
            if not text or not text.strip():
                self._flash("edit cancelled"); return
            try:
                self.mem.supersede(mid, text)
                self._flash("edited #" + str(mid))
                self._reload_all()
            except Exception as e:
                self._flash("edit failed: " + str(e), "error")
        self._open_modal("edit memory #" + str(mid) + "   (ctrl-s save · esc cancel)", m.get("content") or "", commit)

    def act_add(self):
        def commit(text):
            if not text or not text.strip():
                self._flash("add cancelled"); return
            try:
                res = self.mem.remember(text, source="mem-browser")
                new_id = res.get("id") if isinstance(res, dict) else None
                tail = (" #" + str(new_id)) if new_id else ""
                self._flash("added" + tail)
                self._reload_all()
            except Exception as e:
                self._flash("add failed: " + str(e), "error")
        self._open_modal("new memory   (ctrl-s save · esc cancel)", "", commit)

    def act_trigger_add(self):
        m = self.current()
        if not m: return
        mid = m["id"]
        def commit(text):
            if not text or not text.strip():
                self._flash("trigger add cancelled"); return
            try:
                self.mem.trigger_add(mid, text.strip())
                self._flash("trigger added")
                self._reload_triggers()
            except Exception as e:
                self._flash("trigger add failed: " + str(e), "error")
        self._open_modal("new trigger for memory #" + str(mid) + "   (ctrl-s save · esc cancel)", "", commit)

    def _trigger_remove_by_id(self, tid):
        try:
            self.mem.trigger_remove(tid)
            self._flash("removed trigger #" + str(tid))
            self._reload_triggers()
        except Exception as e:
            self._flash("trigger remove failed: " + str(e), "error")

    def act_trigger_remove(self):
        if not self.triggers: return
        t = self.triggers[self.trigger_cursor]
        self._trigger_remove_by_id(t.get("id"))

    # ---- build ---------------------------------------------------------
    def build(self):
        kb = KeyBindings()
        in_list = Condition(lambda: self.focus == "list")
        in_trig = Condition(lambda: self.focus == "triggers")
        in_input = Condition(lambda: self.focus == "input")
        in_filter = Condition(lambda: self.focus == "filter")
        is_modal = Condition(lambda: self.modal_active)
        not_modal = Condition(lambda: not self.modal_active)
        is_help = Condition(lambda: self.show_help)
        not_help = Condition(lambda: not self.show_help)
        no_modal_no_help = not_modal & not_help

        # ---- modal keys ----
        @kb.add("escape", filter=is_modal)
        def _(event): self._close_modal(commit=False)

        @kb.add("c-s", filter=is_modal)
        def _(event): self._close_modal(commit=True)

        @kb.add("c-c", filter=is_modal)
        def _(event): self._close_modal(commit=False)

        # ---- help toggle ----
        @kb.add("?", filter=not_modal)
        def _(event):
            self.show_help = not self.show_help

        @kb.add("escape", filter=not_modal & is_help)
        def _(event):
            self.show_help = False

        # ---- focus management ----
        @kb.add("tab", filter=no_modal_no_help)
        def _(event):
            order = ["list", "triggers", "filter"]
            try:
                i = order.index(self.focus)
            except ValueError:
                i = 0
            self.focus = order[(i + 1) % len(order)]
            self._update_focus()

        @kb.add("s-tab", filter=no_modal_no_help)
        def _(event):
            order = ["list", "triggers", "filter"]
            try:
                i = order.index(self.focus)
            except ValueError:
                i = 0
            self.focus = order[(i - 1) % len(order)]
            self._update_focus()

        @kb.add("escape", filter=no_modal_no_help & in_filter)
        def _(event):
            self.filter_buffer.text = ""
            self.focus = "list"
            self._update_focus()

        @kb.add("escape", filter=no_modal_no_help & in_list)
        def _(event):
            if self.filter_buffer.text:
                self.filter_buffer.text = ""
            else:
                event.app.exit()

        @kb.add("/", filter=no_modal_no_help & in_list)
        def _(event):
            self.focus = "filter"
            self._update_focus()

        @kb.add("q", filter=no_modal_no_help & in_list)
        def _(event): event.app.exit()

        # ---- list nav ----
        for k in ("j", "down"):
            @kb.add(k, filter=no_modal_no_help & in_list)
            def _(event):
                if self.memories:
                    self.cursor = (self.cursor + 1) % len(self.memories)
                    self._reload_triggers()

        for k in ("k", "up"):
            @kb.add(k, filter=no_modal_no_help & in_list)
            def _(event):
                if self.memories:
                    self.cursor = (self.cursor - 1) % len(self.memories)
                    self._reload_triggers()

        @kb.add("g", "g", filter=no_modal_no_help & in_list)
        def _(event):
            self.cursor = 0; self._reload_triggers()

        @kb.add("G", filter=no_modal_no_help & in_list)
        def _(event):
            if self.memories:
                self.cursor = len(self.memories) - 1; self._reload_triggers()

        @kb.add("pageup", filter=no_modal_no_help & in_list)
        def _(event):
            if self.memories:
                self.cursor = max(0, self.cursor - 10); self._reload_triggers()

        @kb.add("pagedown", filter=no_modal_no_help & in_list)
        def _(event):
            if self.memories:
                self.cursor = min(len(self.memories) - 1, self.cursor + 10); self._reload_triggers()

        # ---- actions (list focus) ----
        @kb.add("space", filter=no_modal_no_help & in_list)
        def _(event): self.act_mark_toggle()

        @kb.add("d", filter=no_modal_no_help & in_list)
        def _(event): self.act_deprecate()

        @kb.add("D", filter=no_modal_no_help & in_list)
        def _(event): self.act_forget()

        @kb.add("P", filter=no_modal_no_help & in_list)
        def _(event): self.act_purge_marked()

        @kb.add("u", filter=no_modal_no_help & in_list)
        def _(event): self.act_undo()

        @kb.add("p", filter=no_modal_no_help & in_list)
        def _(event): self.act_pin()

        @kb.add("e", filter=no_modal_no_help & in_list)
        def _(event): self.act_edit()

        @kb.add("+", filter=no_modal_no_help & in_list)
        def _(event): self.act_add()

        @kb.add("t", filter=no_modal_no_help & in_list)
        def _(event): self.act_trigger_add()

        @kb.add("enter", filter=no_modal_no_help & in_list)
        @kb.add("right", filter=no_modal_no_help & in_list)
        def _(event):
            self.focus = "triggers"
            self._update_focus()

        @kb.add("r", filter=no_modal_no_help & in_list)
        def _(event):
            self._reload_all(); self._flash("refreshed")

        # ---- trigger pane ----
        @kb.add("J", filter=no_modal_no_help & in_trig)
        @kb.add("j", filter=no_modal_no_help & in_trig)
        @kb.add("down", filter=no_modal_no_help & in_trig)
        def _(event):
            if self.triggers:
                self.trigger_cursor = (self.trigger_cursor + 1) % len(self.triggers)

        @kb.add("K", filter=no_modal_no_help & in_trig)
        @kb.add("k", filter=no_modal_no_help & in_trig)
        @kb.add("up", filter=no_modal_no_help & in_trig)
        def _(event):
            if self.triggers:
                self.trigger_cursor = (self.trigger_cursor - 1) % len(self.triggers)

        @kb.add("T", filter=no_modal_no_help & in_trig)
        @kb.add("x", filter=no_modal_no_help & in_trig)
        @kb.add("d", filter=no_modal_no_help & in_trig)
        def _(event): self.act_trigger_remove()

        @kb.add("+", filter=no_modal_no_help & in_trig)
        @kb.add("t", filter=no_modal_no_help & in_trig)
        def _(event): self.act_trigger_add()

        @kb.add("left", filter=no_modal_no_help & in_trig)
        @kb.add("escape", filter=no_modal_no_help & in_trig)
        def _(event):
            self.focus = "list"
            self._update_focus()

        # ---- layout ----
        list_ctrl = FormattedTextControl(text=self._list_text, focusable=True, show_cursor=False)
        details_ctrl = FormattedTextControl(text=self._details_text, focusable=True, show_cursor=False)
        footer_ctrl = FormattedTextControl(text=self._footer_text, focusable=False)
        title_ctrl = FormattedTextControl(text=lambda: [("class:title", "  swiszCLI /mem browser   ")], focusable=False)
        help_ctrl = FormattedTextControl(text=self._help_text, focusable=False)
        filter_prompt_ctrl = FormattedTextControl(text=self._filter_prompt, focusable=False)
        modal_label_ctrl = FormattedTextControl(text=lambda: [("class:dialog frame.label", "  " + self.modal_title + "  ")], focusable=False)

        filter_window = Window(BufferControl(buffer=self.filter_buffer, focusable=True), height=1, style="class:filter")
        self.list_window = Window(list_ctrl, wrap_lines=False, always_hide_cursor=True)
        self.right_window = Window(details_ctrl, wrap_lines=True, always_hide_cursor=True)
        modal_input_window = Window(BufferControl(buffer=self.modal_buffer, focusable=True), height=D(min=3, preferred=8), wrap_lines=True, style="class:edit-input")

        self.filter_window = filter_window

        body = VSplit([
            self.list_window,
            Window(width=1, char="│"),
            self.right_window,
        ])

        modal_box = Frame(
            HSplit([
                Window(modal_label_ctrl, height=1),
                modal_input_window,
                Window(FormattedTextControl(text=lambda: [("class:help", "  ctrl-s save   esc cancel")], focusable=False), height=1),
            ]),
            title=None,
        )

        filter_bar = VSplit([
            Window(filter_prompt_ctrl, width=lambda: len(self._filter_prompt()[0][1])),
            filter_window,
        ], height=1)

        root_body = HSplit([
            Window(title_ctrl, height=1),
            filter_bar,
            ConditionalContainer(Window(help_ctrl, wrap_lines=True), filter=is_help),
            ConditionalContainer(body, filter=not_help),
            Window(height=1, char="─"),
            Window(footer_ctrl, height=2),
        ])

        root = FloatContainer(
            content=root_body,
            floats=[
                Float(
                    content=ConditionalContainer(modal_box, filter=is_modal),
                    top=3, left=4, right=4,
                ),
            ],
        )

        self._app = Application(
            layout=Layout(root, focused_element=self.list_window),
            key_bindings=kb,
            style=_STYLE,
            full_screen=True,
            mouse_support=True,
        )
        # cache references for focus switching
        self._win_list = self.list_window
        self._win_filter = filter_window
        self._win_right = self.right_window
        self._win_modal = modal_input_window
        return self._app

    def _update_focus(self):
        if self._app is None: return
        try:
            if self.focus == "filter":
                self._app.layout.focus(self._win_filter)
            elif self.focus == "triggers":
                self._app.layout.focus(self._win_right)
            elif self.focus == "input":
                self._app.layout.focus(self._win_modal)
            else:
                self._app.layout.focus(self._win_list)
        except Exception:
            pass


def run_mem_browser(mem_client, initial_query=""):
    browser = MemBrowser(mem_client)
    if initial_query:
        browser.filter_buffer.text = initial_query
    app = browser.build()
    # If modal opens, the focus follower kicks in via key handler; ensure initial focus on list.
    browser._update_focus()
    app.run()
