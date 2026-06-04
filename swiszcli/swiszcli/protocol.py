"""Strict tool-call wire protocol for small local models.

Why not JSON tool calls: 9B models drift on JSON schemas under pressure.
Instead we use a sentinel-delimited single-tool protocol the model
literally cannot mis-schema, because there IS no schema beyond:

    <<SWISZ>>
    free-form swiszard DSL task on one or more lines
    <<END>>

The model is told it has ONE tool. The router (swiszard) is the agent
behind that tool. The model job is composition + intent, not schema.

Anything outside the sentinels is treated as the assistant reply text.
Multiple calls per turn are allowed; we execute them in order, feed each
result back, and re-prompt until the model emits no more calls or until
max_iters is hit (fail loud).

HONESTY GUARDS (added 2026-06-02 after a model fabricated <<SWISZ_RESULT>>
blocks instead of issuing real calls):

  1. <<SWISZ_RESULT ... <<END_RESULT>> is a RESERVED harness-only sentinel.
     extract_fabricated_results() finds any such block, scrub_fabricated_results()
     strips them. The agent loop checks for fabrications after every stream
     and refuses to commit them to history.

  2. Every real tool result is stamped with a server-issued nonce id=ar_XXXX.
     A block without a *live* nonce (one the harness minted this turn) is
     by definition fabricated. The model literally cannot guess the nonce,
     so this makes fabrication structurally non-smugglable.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

# Permissive on whitespace, strict on the sentinels themselves.
CALL_RE = re.compile(
    r"<<\s*SWISZ\s*>>\s*(?P<body>.*?)\s*<<\s*END\s*>>",
    re.DOTALL | re.IGNORECASE,
)

# Reserved RESULT sentinel — only the harness may emit these. Captures
# the (optional) id=ar_xxx nonce so the agent can distinguish live from
# fabricated/stale blocks.
RESULT_RE = re.compile(
    r"<<\s*SWISZ_RESULT\b(?P<attrs>[^>]*)>>\s*(?P<body>.*?)\s*<<\s*END_RESULT\s*>>",
    re.DOTALL | re.IGNORECASE,
)
NONCE_RE = re.compile(r"\bid\s*=\s*(?P<id>ar_[a-f0-9]+)", re.IGNORECASE)


def mint_nonce() -> str:
    """Mint a fresh result nonce. Model cannot guess these."""
    return "ar_" + secrets.token_hex(6)


@dataclass
class ToolCall:
    raw: str
    task: str
    start: int
    end: int


@dataclass
class FabricatedResult:
    raw: str
    attrs: str
    nonce: str | None
    start: int
    end: int


def _unwrap_angle(body: str) -> str:
    if len(body) >= 2 and body[0] == "<" and body[-1] == ">" and ">" not in body[1:-1]:
        return body[1:-1].strip()
    return body


def extract_calls(text: str) -> list[ToolCall]:
    out: list[ToolCall] = []
    for m in CALL_RE.finditer(text):
        body = (m.group("body") or "").strip()
        body = _unwrap_angle(body)
        if not body:
            continue
        out.append(ToolCall(raw=m.group(0), task=body, start=m.start(), end=m.end()))
    return out


def strip_calls(text: str) -> str:
    return CALL_RE.sub("", text).strip()


def extract_fabricated_results(text: str, *, live_nonces: set[str] | None = None) -> list[FabricatedResult]:
    """Find <<SWISZ_RESULT>> blocks in MODEL output. Any block whose nonce is
    not in live_nonces is treated as fabricated. If live_nonces is None,
    EVERY result block in the text is treated as fabricated (the model has
    no business emitting them at all — only the harness does)."""
    out: list[FabricatedResult] = []
    for m in RESULT_RE.finditer(text):
        attrs = m.group("attrs") or ""
        nm = NONCE_RE.search(attrs)
        nonce = nm.group("id") if nm else None
        if live_nonces is not None and nonce in live_nonces:
            continue
        out.append(FabricatedResult(raw=m.group(0), attrs=attrs.strip(),
                                    nonce=nonce, start=m.start(), end=m.end()))
    return out


def scrub_fabricated_results(text: str, *, live_nonces: set[str] | None = None) -> tuple[str, list[FabricatedResult]]:
    """Remove fabricated result blocks from text. Returns (clean_text, removed_list)."""
    removed = extract_fabricated_results(text, live_nonces=live_nonces)
    if not removed:
        return text, []
    # Remove from the end so offsets stay valid
    clean = text
    for fab in sorted(removed, key=lambda f: f.start, reverse=True):
        clean = clean[:fab.start] + clean[fab.end:]
    return clean.strip(), removed


def format_tool_result(task: str, result: str,
                       *, archive_ref=None,
                       max_chars: int = 8000,
                       nonce: str | None = None) -> str:
    """Format a tool result for re-injection.

    If a nonce is supplied it is embedded as id=ar_XXXX in the opening
    sentinel so the agent loop can distinguish live (harness-minted)
    blocks from fabricated/stale ones. Backwards compatible: when nonce
    is None we still emit a result block (without an id=) — used by the
    legacy test suite. New callers in agent.py always pass a nonce.
    """
    body = result
    if len(body) > max_chars:
        suffix = f"\n... [truncated, {len(result) - max_chars} more chars"
        if archive_ref:
            suffix += f"; full body at {archive_ref}"
        suffix += "]"
        body = body[:max_chars] + suffix
    if nonce is None:
        header = f"<<SWISZ_RESULT task={task!r}>>"
    else:
        header = f"<<SWISZ_RESULT id={nonce} task={task!r}>>"
    return f"{header}\n{body}\n<<END_RESULT>>"


# ── Stream-time fabrication filter ──────────────────────────────────────
# Used by Agent._stream_one to hide fabricated <<SWISZ_RESULT>> blocks from
# the terminal AS they stream, not after the fact. Display-layer only; the
# post-stream history scrubber still runs and is the source of truth for
# what enters conversation history.

_SENTINEL_OPEN = "<<SWISZ_RESULT"
_SENTINEL_CLOSE = "<<END_RESULT>>"


class StreamFabFilter:
    """Incremental filter: feed tokens, get back what's safe to display.

    State machine:
      NORMAL   → tokens pass through, except we withhold any tail that
                 could still grow into "<<SWISZ_RESULT". When we see a
                 complete "<<SWISZ_RESULT...>>" header, optionally check
                 the nonce against live_nonces; if absent/unknown, switch
                 to SUPPRESS and emit a single red marker.
      SUPPRESS → swallow everything until we see "<<END_RESULT>>", then
                 return to NORMAL.

    Any <<SWISZ_RESULT>> in MODEL stream is structurally suspect: real
    results arrive as separate user-role messages, NOT in the chat stream.
    So even with a real-looking nonce, suppression is the right move at
    display time — the post-stream scrubber decides what enters history.
    """

    MARKER = "\n[⚠ fabrication suppressed — model tried to emit a fake tool result]\n"

    def __init__(self, live_nonces: set[str] | None = None) -> None:
        self.live_nonces = live_nonces or set()
        self.buf = ""
        self.suppress = False
        self.fab_count = 0

    def _safe_normal_emit_len(self) -> int:
        """In NORMAL mode, how many chars from self.buf are safe to flush?

        We hold back the longest suffix that could be a prefix of the
        opening sentinel, so we never display the first '<' of a
        sentinel and only later realize we should have hidden it.
        """
        n = len(self.buf)
        max_hold = len(_SENTINEL_OPEN)
        # Find longest suffix of buf that is a prefix of _SENTINEL_OPEN.
        # Anything beyond that is safe.
        start = max(0, n - max_hold + 1)
        for i in range(start, n + 1):
            tail = self.buf[i:]
            if _SENTINEL_OPEN.startswith(tail):
                return i
        return n

    def feed(self, tok: str) -> str:
        """Append tok to buffer and return whatever is safe to display now."""
        self.buf += tok
        out_parts: list[str] = []
        while True:
            if self.suppress:
                idx = self.buf.find(_SENTINEL_CLOSE)
                if idx == -1:
                    # Still inside fab block — emit nothing, keep buffering
                    # but drop the bytes we've already committed to suppress
                    # so the buffer doesn't grow unbounded.
                    if len(self.buf) > len(_SENTINEL_CLOSE):
                        # Keep last len-1 chars (might be partial close)
                        self.buf = self.buf[-(len(_SENTINEL_CLOSE) - 1):]
                    return "".join(out_parts)
                # Found close — consume through it and resume normal.
                self.buf = self.buf[idx + len(_SENTINEL_CLOSE):]
                self.suppress = False
                continue

            # NORMAL: look for a complete fabricated header "<<SWISZ_RESULT...>>"
            open_idx = self.buf.find(_SENTINEL_OPEN)
            if open_idx == -1:
                # No sentinel at all — flush the safe portion, hold the rest.
                safe = self._safe_normal_emit_len()
                if safe > 0:
                    out_parts.append(self.buf[:safe])
                    self.buf = self.buf[safe:]
                return "".join(out_parts)

            # Sentinel start found. Need the closing ">>" of the HEADER
            # to make a fab/real decision based on nonce.
            close_idx = self.buf.find(">>", open_idx + len(_SENTINEL_OPEN))
            if close_idx == -1:
                # Header still streaming — emit text before the sentinel,
                # hold the rest.
                if open_idx > 0:
                    out_parts.append(self.buf[:open_idx])
                    self.buf = self.buf[open_idx:]
                return "".join(out_parts)

            # Full header in hand. Extract nonce, decide.
            header = self.buf[open_idx:close_idx + 2]
            nm = NONCE_RE.search(header)
            nonce = nm.group("id") if nm else None
            fabricated = (nonce is None) or (nonce not in self.live_nonces)

            # Emit any safe text before the sentinel.
            if open_idx > 0:
                out_parts.append(self.buf[:open_idx])

            if fabricated:
                # Suppress: emit marker, drop header, enter SUPPRESS.
                self.fab_count += 1
                out_parts.append(self.MARKER)
                self.buf = self.buf[close_idx + 2:]
                self.suppress = True
                continue
            else:
                # Real result block (rare in model stream). Let it through
                # — the post-stream history scrubber will validate it again.
                out_parts.append(self.buf[open_idx:close_idx + 2])
                self.buf = self.buf[close_idx + 2:]
                continue

    def flush(self) -> str:
        """Stream ended. Emit any leftover safe bytes."""
        if self.suppress:
            # Stream cut off mid-fabrication. Drop the remainder silently.
            self.buf = ""
            return ""
        out = self.buf
        self.buf = ""
        return out
