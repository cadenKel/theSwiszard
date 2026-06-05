"""Shared wizard types — minimal dataclasses that break the circular dependency
between swiszproj (needs wizard shapes) and swiszcli (owns the real wizard engine).

swiszcli/wizard.py has the REAL Wizard, Step, Choice, and REGISTRY.
These stubs let swiszproj define wizards without importing swiszcli.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

@dataclass
class Choice:
    label: str
    value: Any = None
    next: Optional[Callable] = None

@dataclass
class Step:
    prompt: str
    choices: list = field(default_factory=list)
    default: Any = None

class Wizard:
    def __init__(self, steps: list):
        self.steps = steps

def register(wizard, name: str):
    pass  # registration is a no-op in swiszproj; swiszcli handles its own registry
