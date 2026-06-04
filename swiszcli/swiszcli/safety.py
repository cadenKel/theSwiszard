"""
safety.py — destructive-verb pre-pass for swiszard calls (phase 7d).

Deterministic regex. Marks each task as 'safe' | 'destructive' with a list
of matched reasons. The agent loop uses verdict() to decide whether to
auto-execute or to inject a confirmation step.

No fallbacks. No quiet bypass. The only way to skip is an explicit opt-in
flag (used by wizards that have already collected user consent).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Patterns are intentionally broad. False-positive = one extra confirm.
# False-negative = unrecoverable damage. We bias toward false-positives.
DESTRUCTIVE_PATTERNS: list[tuple[str, re.Pattern]] = [
	("rm -rf",         re.compile(r"\brm\s+(-[rRfF]+\s+|--recursive\s+|--force\s+)")),
	("rm /",           re.compile(r"\brm\s+(-\S+\s+)*/(?!tmp|home)\S*")),
	("dd",             re.compile(r"\bdd\s+(if=|of=)")),
	("mkfs",           re.compile(r"\bmkfs(\.\w+)?\b")),
	("fdisk/parted",   re.compile(r"\b(fdisk|parted|wipefs|sgdisk)\b")),
	("dropdb/dropuser",re.compile(r"\b(dropdb|dropuser|DROP\s+(TABLE|DATABASE|SCHEMA))\b", re.I)),
	("sudo",           re.compile(r"\bsudo\b")),
	("curl | sh",      re.compile(r"\bcurl\b[^|]*\|\s*(sh|bash|zsh|sudo)")),
	("wget | sh",      re.compile(r"\bwget\b[^|]*\|\s*(sh|bash|zsh|sudo)")),
	("> /etc",         re.compile(r">\s*/etc/")),
	("> /boot",        re.compile(r">\s*/boot/")),
	("> /usr",         re.compile(r">\s*/usr/")),
	("chmod 777 /",    re.compile(r"\bchmod\s+(-R\s+)?[0-7]*7{2,3}\s+/")),
	("chown -R /",     re.compile(r"\bchown\s+-R\s+\S+\s+/(?!tmp|home)")),
	("git push -f",    re.compile(r"\bgit\s+push\s+(.+\s+)?(-f|--force)")),
	("git reset --hard",re.compile(r"\bgit\s+reset\s+--hard\b")),
	("systemctl stop/disable", re.compile(r"\bsystemctl\s+(--user\s+)?(stop|disable|mask|kill)\b")),
	("kill -9 pid",    re.compile(r"\bkill\s+-9\s+\d+")),
	("memory forget",  re.compile(r"\bmemory\s+forget\b")),
	("truncate",       re.compile(r"\btruncate\b")),
]


@dataclass
class Verdict:
	destructive: bool
	reasons: list[str] = field(default_factory=list)

	def summary(self) -> str:
		if not self.destructive:
			return "safe"
		return "destructive: " + ", ".join(self.reasons)


def verdict(task: str) -> Verdict:
	"""Inspect a swiszard task string. Return Verdict."""
	reasons: list[str] = []
	for label, pat in DESTRUCTIVE_PATTERNS:
		if pat.search(task):
			reasons.append(label)
	return Verdict(destructive=bool(reasons), reasons=reasons)


def is_safe_prefix(task: str) -> bool:
	"""Tasks beginning with with safety:/route: prefixes or 'help' are always safe."""
	t = task.lstrip().lower()
	return t.startswith("safety:") or t.startswith("route:") or t == "help"
