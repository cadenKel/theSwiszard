"""Runtime config for swiszcli.

All defaults are chosen for the local setup:
  - Ollama on http://127.0.0.1:11434
	: model qwen3.5:9b-caden
  - Swiszmem on http://127.0.0.1:7437
  - Swiszard package imported directly (no MCP hop)

Overrides via env vars:
  SWISZCLI_MODEL, SWISZCLI_OLLAMA_URL, SWISZCLI_MEM_URL,
  SWISZCLI_SWISZARD_PATH, SWISZCLI_CTX_TURNS, SWISZCLI_STATE_DIR.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .env import load_swiszcli_env


DEFAULT_SWISZARD_PATH = "/home/ziggibot/swiszard"

# Load swiszcli .env so os.environ picks up API keys
load_swiszcli_env()


@dataclass
class Config:
    model: str = field(default_factory=lambda: os.environ.get("SWISZCLI_MODEL", "qwen3.5:9b-caden-fast"))
    ollama_url: str = field(default_factory=lambda: os.environ.get("SWISZCLI_OLLAMA_URL", "http://127.0.0.1:11434"))
    mem_url: str = field(default_factory=lambda: os.environ.get("SWISZCLI_MEM_URL", "http://127.0.0.1:7437"))
    swiszard_path: str = field(default_factory=lambda: os.environ.get("SWISZCLI_SWISZARD_PATH", DEFAULT_SWISZARD_PATH))
    ctx_turns: int = field(default_factory=lambda: int(os.environ.get("SWISZCLI_CTX_TURNS", "12")))
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("SWISZCLI_STATE_DIR", str(Path.home() / ".swiszcli"))))
    max_tool_iters: int = 16
    # safety_mode: "confirm" (interactive y/N), "block" (always block), "off" (allow all — DANGEROUS)
    safety_mode: str = "confirm"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def provider(self):
        """Which LLM provider: 'ollama', 'deepseek', or 'openrouter'."""
        return os.environ.get("SWISZCLI_PROVIDER", "ollama").strip().lower()

    @property
    def provider_api_key(self):
        """API key for current provider from os.environ (populated by .env)."""
        p = self.provider
        if p == "ollama":
            return "ollama"
        if p == "deepseek":
            return os.environ.get("DEEPSEEK_API_KEY", "")
        if p == "openrouter":
            return os.environ.get("OPENROUTER_API_KEY", "")
        return ""

    @property
    def provider_base_url(self):
        """Base URL for provider's OpenAI-compatible endpoint."""
        p = self.provider
        if p == "ollama":
            return self.ollama_url.rstrip("/") + "/v1"
        if p == "deepseek":
            return os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        if p == "openrouter":
            return os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/v1").rstrip("/")
        return ""
