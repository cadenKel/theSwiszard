"""Model warm-up: preload into VRAM with long keep_alive so first turn is fast.

Hard rule (no fallbacks): if ollama errors, we surface it. No silent retry.
"""
from __future__ import annotations

import threading
import time
import sys
import httpx


def preload(base_url: str, model: str, keep_alive: str = "24h", timeout: float = 600.0) -> dict:
	"""Block until model is resident in VRAM. Returns timing dict.

	Uses /api/chat with empty messages — the canonical preload signal
	in ollama. /api/generate with prompt:"" is short-circuited (returns 3ms)
	and does NOT trigger a real load.
	"""
	url = base_url.rstrip("/") + "/api/chat"
	body = {"model": model, "messages": [], "keep_alive": keep_alive, "stream": False}
	t0 = time.monotonic()
	with httpx.Client(timeout=timeout) as c:
		r = c.post(url, json=body)
		r.raise_for_status()
	dt = time.monotonic() - t0
	return {"load_s": dt}


def unload(base_url: str, model: str, timeout: float = 30.0) -> None:
	"""Evict from VRAM (keep_alive=0 forces immediate unload)."""
	url = base_url.rstrip("/") + "/api/chat"
	body = {"model": model, "messages": [], "keep_alive": 0, "stream": False}
	with httpx.Client(timeout=timeout) as c:
		r = c.post(url, json=body)
		r.raise_for_status()


def is_resident(base_url: str, model: str, timeout: float = 5.0) -> bool:
	with httpx.Client(timeout=timeout) as c:
		r = c.get(base_url.rstrip("/") + "/api/ps")
		r.raise_for_status()
		for m in r.json().get("models", []):
			if m.get("name") == model or m.get("model") == model:
				return m.get("size_vram", 0) > 0
	return False


class Spinner:
	"""Tiny stderr spinner with elapsed seconds."""
	def __init__(self, label: str):
		self.label = label
		self._stop = threading.Event()
		self._t = None
		self._t0 = 0.0

	def start(self):
		self._t0 = time.monotonic()
		self._t = threading.Thread(target=self._run, daemon=True)
		self._t.start()
		return self

	def _run(self):
		frames = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
		i = 0
		while not self._stop.is_set():
			dt = time.monotonic() - self._t0
			sys.stderr.write(f"\r  {frames[i % len(frames)]} {self.label}  {dt:5.1f}s")
			sys.stderr.flush()
			i += 1
			time.sleep(0.1)

	def stop(self, done_label: str = ""):
		self._stop.set()
		if self._t:
			self._t.join()
		dt = time.monotonic() - self._t0
		sys.stderr.write(f"\r  \u2713 {done_label or self.label}  {dt:.1f}s\033[K\n")
		sys.stderr.flush()
