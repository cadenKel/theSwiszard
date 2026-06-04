"""Agent loop: model emits swiszard calls, we execute, feed results back.

Hard rules (no fallbacks, fail loud):
  - Tool errors are reported back to the model as the result body (so it
    can recover), but Python exceptions in our code bubble up.
  - max_tool_iters caps runaway loops; hitting it raises.
  - No truncation of model output beyond the explicit format_tool_result cap.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from .protocol import extract_calls, format_tool_result, strip_calls, scrub_fabricated_results, mint_nonce, StreamFabFilter
from .identity import to_render as _identity_to_render
from .safety import verdict as safety_verdict, is_safe_prefix


@dataclass
class Turn:
    role: str        # "user" | "assistant"
    content: str

    def to_msg(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class AgentState:
    system_prompt: str
    history: list[Turn] = field(default_factory=list)
    ctx_turns: int = 12   # number of (user+assistant) pairs to keep in prompt

    def messages_for_model(self, extra_system: str = "") -> list[dict[str, str]]:
        sys_content = self.system_prompt
        if extra_system:
            sys_content = sys_content + "\n\n" + extra_system
        msgs: list[dict[str, str]] = [{"role": "system", "content": sys_content}]
        # Keep last N turns (user+assistant interleaved). 2*ctx_turns messages max.
        tail = self.history[-(2 * self.ctx_turns):]
        msgs.extend(t.to_msg() for t in tail)
        return msgs


class Agent:
    def __init__(
        self,
        *,
        state: AgentState,
        chat_stream: Callable,        # (messages) -> Iterator[str]
        swiszard_do: Callable[[str], str],
        on_token: Callable[[str], None] | None = None,
        on_tool_start: Callable[[str], None] | None = None,
        on_tool_end: Callable[[str, str, float], None] | None = None,
        recall_fn: Callable[[str], list[dict]] | None = None,
        memory_renderer: Callable[[list[dict]], str] | None = None,
        archive=None,
        max_tool_iters: int = 16,
        confirm_destructive=None,
        post_stream_check=None,   # P1.5: (draft_text) -> (revised_extra_system | None)
    ) -> None:
        self.state = state
        self.chat_stream = chat_stream
        self.swiszard_do = swiszard_do
        self.on_token = on_token or (lambda s: sys.stdout.write(s) or sys.stdout.flush())
        self.on_tool_start = on_tool_start or (lambda t: None)
        self.on_tool_end = on_tool_end or (lambda t, r, d: None)
        self.recall_fn = recall_fn
        self.memory_renderer = memory_renderer
        self.archive = archive
        self.max_tool_iters = max_tool_iters
        self.confirm_destructive = confirm_destructive
        self.post_stream_check = post_stream_check
        # Live nonces minted for the current turn. Result blocks the model
        # emits without a matching live nonce are treated as fabrication.
        self._live_nonces: set[str] = set()

    def _stream_one(self, extra_system: str = "") -> str:
        msgs = self.state.messages_for_model(extra_system=extra_system)
        buf: list[str] = []
        # Stream-time fabrication filter: hides fake <<SWISZ_RESULT>> blocks
        # from the terminal AS they stream, so Sean never sees invented output.
        # The post-stream scrubber below is still authoritative for history.
        sff = StreamFabFilter(live_nonces=self._live_nonces)
        for tok in self.chat_stream(msgs):
            buf.append(tok)
            safe = sff.feed(tok)
            if safe:
                self.on_token(safe)
        tail = sff.flush()
        if tail:
            self.on_token(tail)
        raw = "".join(buf)
        # Honesty guard: strip any <<SWISZ_RESULT>> blocks the model fabricated.
        # Only blocks whose id= matches a nonce the harness minted this turn
        # are real. Anything else is invention and must NOT enter history.
        clean, fabs = scrub_fabricated_results(raw, live_nonces=self._live_nonces)
        if fabs:
            ids = [f.nonce or "<no-id>" for f in fabs]
            try:
                # sff already showed the live marker; this is the post-stream
                # confirmation with concrete ids for the user log.
                self.on_token(f"\n[fabrication] stripped {len(fabs)} fake <<SWISZ_RESULT>> block(s) ids={ids}\n")
            except Exception:
                pass
        return clean

    def turn(self, user_text: str) -> str:
        """Run one full user→assistant turn (including any tool loops).

        Returns the final assistant reply text (with tool blocks stripped).
        """
        # Fresh nonce allowlist per turn. Stale nonces from a prior turn
        # do NOT validate a model emission this turn — keeps the model from
        # replaying old result blocks to look productive.
        self._live_nonces = set()
        # Optional proactive memory injection (swizmem-style)
        extra_system = ""
        if self.recall_fn and self.memory_renderer:
            try:
                mems = self.recall_fn(user_text)
                if mems:
                    extra_system = self.memory_renderer(mems)
            except Exception as e:
                # Recall failures are NOT fatal — but they ARE visible.
                extra_system = f"<recall-error>{e}</recall-error>"
        # P1.6a: expand {{user}} sentinel to configured real name at the
        # final READ chokepoint, so storage stays portable.
        extra_system = _identity_to_render(extra_system)

        self.state.history.append(Turn("user", user_text))

        # First response. Stream tokens to the user.
        assistant_text = self._stream_one(extra_system=extra_system)

        # P1.5 gap detector hook: only runs when there are NO tool calls (final draft).
        if self.post_stream_check is not None:
            try:
                from .protocol import extract_calls as _xc
                if not _xc(assistant_text):
                    retry_extra = self.post_stream_check(assistant_text)
                    if retry_extra:
                        # Push the gapped draft as an assistant turn, then user-message
                        # with the research, then re-stream. One retry max.
                        self.state.history.append(Turn("assistant", assistant_text))
                        self.state.history.append(Turn("user", retry_extra))
                        assistant_text = self._stream_one(extra_system=extra_system)
            except Exception as _e:
                print(f"[gap-detector] failed: {_e}")

        iters = 0
        while True:
            calls = extract_calls(assistant_text)
            if not calls:
                break
            iters += 1
            if iters > self.max_tool_iters:
                raise RuntimeError(
                    f"max_tool_iters={self.max_tool_iters} exceeded; model in a tool-call loop"
                )

            # Append the assistant message containing the calls to history
            # BEFORE we send results back — preserves causality for the model.
            self.state.history.append(Turn("assistant", assistant_text))

            # Run each call in order, feeding results back as ONE user message.
            result_blocks: list[str] = []
            for call in calls:
                import time
                self.on_tool_start(call.task)
                t0 = time.monotonic()
                sv = None if is_safe_prefix(call.task) else safety_verdict(call.task)
                blocked = False
                if sv is not None and sv.destructive:
                    if self.confirm_destructive is None:
                        result = ("BLOCKED by safety gate (no confirm handler installed). "
                                  f"Reasons: {chr(44).join(sv.reasons)}. "
                                  "Use safety: prefix to preview, or install a confirm handler.")
                        blocked = True
                    else:
                        try:
                            ok = bool(self.confirm_destructive(call.task, sv))
                        except Exception as ce:
                            result = f"BLOCKED by safety gate (confirm raised: {ce})."
                            ok = False
                            blocked = True
                        if not blocked and not ok:
                            result = ("BLOCKED by safety gate (user declined). "
                                      f"Reasons: {chr(44).join(sv.reasons)}.")
                            blocked = True
                if not blocked:
                    try:
                        result = self.swiszard_do(call.task)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"
                dt = time.monotonic() - t0
                self.on_tool_end(call.task, result, dt)
                ref = None
                if self.archive is not None:
                    try:
                        ref = self.archive.write(call.task, result)
                    except Exception as ae:
                        ref = f"[archive-error: {ae}]"
                nonce = mint_nonce()
                self._live_nonces.add(nonce)
                result_blocks.append(
                    format_tool_result(call.task, result, archive_ref=ref, nonce=nonce))

            results_msg = "\n\n".join(result_blocks)
            self.state.history.append(Turn("user", results_msg))

            # Re-prompt the model with the results
            assistant_text = self._stream_one()

        # Final assistant text (no more tool calls)
        self.state.history.append(Turn("assistant", assistant_text))
        return strip_calls(assistant_text)
