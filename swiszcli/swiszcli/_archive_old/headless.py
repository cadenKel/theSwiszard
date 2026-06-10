"""Headless swiszard agent: factory + runner for non-interactive (MCP) invocations.

make_headless_agent(cfg) returns a HeadlessAgent.
run_headless(spec, timeout) is the high-level entry point.

Architecture mirrors cli.py main() minus PTK: same wizard routing, same recall_fn,
same context assembly. Returns text output as string instead of printing to terminal.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class HeadlessAgent:
    """Headless agent wrapping swiszard's wizard loop."""
    cfg: object  # Config
    mem: object | None = None
    _initialized: bool = field(default=False, repr=False)

    def _init(self):
        if self._initialized:
            return
        try:
            from .memory import Memory
            self.mem = Memory(db_path=self.cfg.state_dir / "memory.db")
        except Exception:
            self.mem = None
        self._initialized = True

    def run(self, spec: str, timeout: int = 120) -> str:
        """Run spec through wizard routing. Returns text output string."""
        self._init()
        buf = io.StringIO()
        result: dict = {"out": ""}
        err_holder: list = []

        def _execute():
            try:
                with redirect_stdout(buf), redirect_stderr(buf):
                    out = self._run_spec(spec)
                result["out"] = out
            except Exception as e:
                result["out"] = f"[err] {type(e).__name__}: {e}"

        t = threading.Thread(target=_execute, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            return f"[err] timeout after {timeout}s"
        captured = buf.getvalue()
        final = result["out"]
        if captured and not final:
            return captured.strip()
        if captured:
            return (final + "\n" + captured.strip()).strip()
        return final

    def _run_spec(self, spec: str) -> str:
        """Core: route spec through swiszard_do (deterministic tier first)."""
        # Tier 1: try deterministic handler first
        try:
            from swiszard.router import swiszard_do
            result = swiszard_do(spec)
            if result and "[dry-run]" not in result and "no route found" not in result.lower():
                return result
        except Exception as e:
            return f"[err] router: {e}"
        # Tier 2: wizard routing (when deterministic fails)
        try:
            from .launch import dispatch_to_wizard
            return dispatch_to_wizard(spec, mem=self.mem, cfg=self.cfg)
        except ImportError:
            pass
        except Exception as e:
            return f"[err] wizard: {e}"
        return f"[err] no handler for: {spec[:120]}"


def make_headless_agent(cfg) -> HeadlessAgent:
    """Factory: creates a HeadlessAgent for the given Config."""
    return HeadlessAgent(cfg=cfg)


def run_headless(spec: str, timeout: int = 120, cfg=None) -> str:
    """High-level entry point for MCP callers. Lazy-loads Config if not provided."""
    if cfg is None:
        try:
            from .config import Config
            cfg = Config()
        except Exception as e:
            return f"[err] config init: {e}"
    agent = make_headless_agent(cfg)
    return agent.run(spec, timeout=timeout)
