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
    key: str = ""
    kind: str = "text"
    choices: list = field(default_factory=list)
    default: Any = None
    pool: str = ""
    new_prompt: str = ""
    next: Optional[Callable] = None
    multiline: bool = False
    validate: Optional[Callable] = None
    help: str = ""
    required: bool = False
    skip_if: Optional[Callable] = None
    placeholder: str = ""

class Wizard:
    def __init__(self, steps: list = None, name: str = "", title: str = "",
                 commit: Any = None, **_kwargs):
        self.name = name
        self.title = title
        self.steps = steps or []
        self.commit = commit

def register(wizard, name: str = ""):
    pass  # registration is a no-op in swiszproj; swiszcli handles its own registry
