# Sean memory-system ideas for swiszCLI (2026-06-01)

Status: draft v3 (two-surface split: swiszContext + swizmem)

Goal: make a small local LLM (qwen3.5:9b class) perform like Claude Code by
letting MEMORY do the bulk of the reasoning. The model is dumb-but-fast;
retrieval does the heavy lifting.

Stack constraints: 12GB VRAM, one ollama, swizmem already running with
nomic-embed-text pinned CPU-only. swiszard is deterministic. NO symbolic
preprocessing (no POS tagging, no SRO triples).


===============================================================
ARCHITECTURE: TWO SURFACES, ONE PIPELINE
===============================================================

swizmem (UNCHANGED)
  - Facts, lessons, corrections, user pins.
  - Things the model needs to FOCUS on.
  - Curated, low volume, high signal per row.
  - Cross-session retrievable.
  - Treat as the moat. Stays precious.

swiszContext (NEW engine)
  - Conversation chunks. Disposable situational background.
  - High volume, low signal per row.
  - Own SQLite db (contexts.db), own table, reuses nomic-embed-text endpoint.
  - Session-scoped retrieval (Sean directive 2026-06-01: 8-turn chunks
    too noisy to cross sessions).
  - Pruning is aggressive: drop chunks older than 30d with
    retrieval_count=0.

The bridge: PROMOTION
  - A chunk retrieved >= 5 times gets promoted.
  - One background LLM call summarizes chunk -> 1-sentence lesson +
    2-3 auto-authored trigger sentences.
  - Lesson lands in swizmem as kind=lesson.
  - Source chunk in swiszContext is marked promoted=1 to prevent
    re-promotion.
  - This is the ONLY write from swiszContext to swizmem. Boundary
    enforceable and auditable.


===============================================================
IDEA A: AUTO-MEMORY EVERYTHING (revised)
===============================================================

REVISED: no turn-pair auto-memories in swizmem. Every turn goes to
swiszContext as part of the rolling chunk_window. swizmem stays curated.

Explicit user override (still high priority):
  /mem add <content> [trigger1; trigger2; trigger3]
- Already wired via mem.* wizards.
- Writes to swizmem with kind=user_pin, higher weight than promoted
  lessons so user-authored facts win ties.


===============================================================
IDEA B: CHUNK_WINDOW + RELEVANCE INJECTION (load-bearing)
===============================================================

Thesis: model never sees full history. Context stays small -> small LLM
stays sharp. Recall does the reasoning. The model does not remember
anything; relevant chunks are handed to it. THIS IS passive reasoning.

Storage in swiszContext (contexts.db):
  schema: id, session_id, turn_start, turn_end, text, embedding,
          retrieval_count, promoted, created_at
  kind:   chunk_window
  window: configurable cfg.live_window_turns (default 8 -- Sean
          directive 2026-06-01, coding-session friendly)
  stride: 2 (sliding window with 6-turn overlap between consecutive
          chunks; compromise between flooding and losing framings)

Retrieval per turn (swiszContext.recall):
  1. Live context = last 8 turns -- plaintext in prompt.
  2. excluded_range = (current_session, current_turn - 7, current_turn).
  3. Query contexts.db for top-K chunk_window by cosine sim to current
     user msg, WHERE session_id = current_session.
  4. Filter out any chunk whose turn_range overlaps excluded_range.
  5. Greedy overlap dedup: walk top-down, drop chunks whose turn_range
     overlaps a chunk already kept. Most relevant wins.
  6. Take top 3 surviving chunks.
  7. Apply recency decay: final_score = cosine * recency^0.3.
  8. Increment retrieval_count on each surfaced chunk (promotion fuel).
  9. Inject above live-last-8 as <recalled-chunks> section.

Separately, swizmem.recall runs as today and feeds <recalled-memory>.
The two sections are visually distinct in the prompt so the model knows
chunks are background and memories are focus items.

No POS tagging. No noun/adjective extraction. nomic-embed-text encodes
topic/subject natively. Query = user message embedded as-is.


===============================================================
SESSION_FRAME (cross-session bookend)
===============================================================

Lives in swiszContext alongside chunk_window but tagged differently.
  kind:   session_frame
  scope:  cross-session retrievable (unlike chunk_window)
  body:   first 2 turns + last 2 turns of a session, embedded as ONE row
  write:  rolling-updated each turn until session ends, then frozen

Purpose: carry the "what was this session about" signal across sessions
without dragging noisy mid-session chunks into unrelated conversations.
The only cross-session conversation memory.


===============================================================
REASONING-FROM-MEMORY ADDITIONS
===============================================================

1. LESSON PROMOTION (the bridge between swiszContext and swizmem)
   chunk_window with retrieval_count >= 5 (Sean directive 2026-06-01)
   triggers a background summarization job. ONE small LLM call, never
   in the turn-loop. Output: kind=lesson row in swizmem with
   auto-authored trigger sentences. This is how raw conversation
   crystallizes into a fact.

2. TOOL-RESULT MEMORIES
   Every swiszard tool result embedded into swiszContext as
   kind=tool_result, task=<dsl_string>. When user asks something
   similar later, recall surfaces past result before model decides
   to re-run the tool. Cuts redundant work hard. Same promotion path:
   if a tool result is retrieved >= 5 times, promote to swizmem
   lesson summarizing what the tool does for what kind of question.

3. NEGATIVE / CORRECTION MEMORIES
   When user message matches correction patterns (no, dont, stop,
   wrong, instead -- deterministic regex), the corrected exchange is
   stored DIRECTLY to swizmem with HIGH weight + kind=correction.
   Skips swiszContext entirely because corrections are too important
   for the lazy-promotion pipeline.

4. PROVENANCE-AWARE INJECTION
   Every injected chunk is labeled in-prompt:
       <chunk turns=42-46 sim=0.81 age=3d count=3>
       ...content...
       </chunk>
   Small models are bad at trusting recall when source is opaque.
   Showing provenance turns recall into citation; the model behaves
   more rigorously and is less likely to confabulate.


===============================================================
IMPLEMENTATION TOUCH POINTS
===============================================================

NEW: swiszcli/swiszcontext/  (or standalone ~/.hermes/plugins/swiszcontext/)
  - __init__.py
  - db.py: contexts.db schema + CRUD
  - engine.py: store_chunk, store_tool_result, store_session_frame,
               recall (session-scoped, overlap-aware, recency-decayed),
               increment_retrieval_count
  - promote.py: background daemon, scans for retrieval_count >= 5,
                summarizes via cheap LLM call, writes lesson to swizmem
  - prune.py: drops chunks > 30d with retrieval_count == 0

swiszcli/agent.py
  - end-of-turn hook: build chunk_window every stride=2 turns,
    write to swiszContext
  - recall_fn: dual query -- swizmem AND swiszContext, render in
    separate prompt sections
  - correction-pattern matcher (deterministic regex) writes to swizmem
  - tool-end hook: write tool result to swiszContext

swiszcli/memory.py
  - stays as-is (swizmem client)
  - add ContextClient as sibling class for swiszContext

swiszcli/cli.py
  - /mem add: already exists, routes to swizmem with kind=user_pin
  - /chunks: debug command, shows which chunks got injected last turn
  - /promote: manual promotion trigger for testing

swiszcli/prompt.py
  - build_memory_block: emits <recalled-memory> from swizmem
  - build_context_block: NEW, emits <recalled-chunks> from swiszContext
  - both injected into system prompt, distinct sections

swizmem (NO schema changes)
  - lessons land as regular memory rows via existing memory.remember API


===============================================================
RESOLVED DIRECTIVES (Sean, 2026-06-01)
===============================================================

D1: Window size default = 8 turns.
D2: Lesson promotion threshold = 5 retrievals.
D3: chunk_window is session-scoped only. session_frame carries
    cross-session continuity instead.
D4: No standalone user-message embeddings. Chunks only. Promotion
    handles compression naturally.
D5: Split into two surfaces -- swiszContext (chunks, disposable) and
    swizmem (facts/lessons/corrections/pins, curated). One bridge:
    promotion at retrieval_count >= 5.


===============================================================
OPEN QUESTIONS
===============================================================

Q5: Correction-pattern regex starter set -- ship hand-authored, or
    learn from /mem add traffic over time?
Q6: Should promotion daemon run on a timer (every N min) or on a
    write-trigger (when retrieval_count hits 5 during a recall)?
Q7: Prune threshold tunable per-user, or hardcoded 30d?
