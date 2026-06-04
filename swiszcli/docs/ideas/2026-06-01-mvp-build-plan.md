# swiszCLI MVP Build Plan v2 (2026-06-01)

Status: simplified. Supersedes v1.

Pre-history: v1 had a 6-layer cascade (Earley, TF-IDF, embeddings, CBR, HMM, Bayes, decision tree). Stress-tested and collapsed:
- Earley duplicated swiszards existing regex dispatcher -> cut
- TF-IDF redundant with nomic embeddings -> cut
- CBR and embedding-prototype routing are the SAME mechanism with different data sources -> merged
- HMM needed weeks of data, CBR handles short-context shorthand -> cut
- Bayes calibrator overkill for 2 voters -> replaced by threshold ladder
- Decision tree debugger is nice-to-have, not MVP -> deferred

Net result: ONE mechanism, not a cascade.


THE ONE MECHANISM
=================

A single sqlite table called examples:

  examples(
    id INTEGER PRIMARY KEY,
    text TEXT,            -- the trigger phrase or learned user input
    embedding BLOB,       -- nomic-embed-text vector
    target TEXT,          -- canonical swiszard DSL string with slots
    source TEXT,          -- seed | learned | github_shared
    weight REAL,          -- starts 0.5, +0.1 on win, -0.2 on correction
    wins INTEGER,
    losses INTEGER,
    last_used TIMESTAMP
  )

Routing flow:
  1. user input arrives
  2. existing swiszard regex dispatcher tries first (unchanged, free win for already-shaped commands)
  3. miss -> embed input, cosine-match against examples table
  4. threshold ladder on top match:
     - score > 0.85 -> execute silently, increment wins
     - score 0.65-0.85 -> execute with one-line preview, "wrong? hit n"
     - score 0.45-0.65 -> one-tap prompt: did you mean X? Y/n
     - score < 0.45 -> fall to LLM, let it propose, confirm, store as learned

Thats it. No cascade. No multi-voter math. One table, one lookup, one feedback loop.


WHY THIS IS ENOUGH
==================

The notepad feel comes from:
- never wrong loud: threshold ladder asks instead of guessing below 0.65
- gets quieter over time: every y/N confirmation adds a row, increases coverage
- understands shorthand: embed (last 2 turns + current input) instead of current alone, so "now do it for bar" resolves via context

The academic name for this is instance-based learning / k-NN with feedback (Aha 1991). Same family as CBR but without the adaptation-step ceremony. Just nearest-neighbor lookup over a growing labeled set.


PHASE 0  --  MVP  (week 1)
==========================

1. swiszContext database
   - new file: swiszcli/context_store.py
   - sqlite at ~/.swiszcli/contexts.db
   - tables: chunks (per memory-ideas doc) + examples (above)

2. Embedding client
   - swiszcli/embed.py, single embed(text) -> List[float]
   - reuse existing nomic endpoint, CPU-pinned, keep_alive=24h
   - batch endpoint for startup seed embedding

3. Examples router
   - swiszcli/router.py
   - each swiszard handler ships 3-5 seed phrases in HANDLER_SEEDS dict
   - on startup: embed seeds, write to examples with source=seed, weight=1.0
   - on input: cosine match, threshold ladder

4. Context recall hook
   - before LLM call: top-k chunks (k=5) injected as <recalled_context>
   - swizmem injection unchanged

Success criteria P0:
- already-shaped commands route via existing dispatcher (zero change there)
- paraphrases of seed phrases route via examples table without LLM
- LLM token usage drops measurably


PHASE 1  --  ONE-TAP LEARNING  (week 2)
=======================================

The examples table starts growing from Sean confirmations.

1. Correction detector
   - heuristic trigger: no/actually/wait/I meant
   - prompt: was that a correction? Y/n
   - on Y: last example weight -= 0.2, store new corrected pair

2. Confirmation on LLM proposals
   - LLM proposes swiszard call -> show: ok? Y/n/edit
   - on Y: store (user_input, proposed_target) with source=learned, weight=0.5
   - on n: if pattern existed, weight -= 0.3

3. Save-worthy moment detector
   - heuristic: I prefer/want/always/never + first person
   - prompt: save this? Y/n
   - on Y: write to swizmem via existing remember path


PHASE 1.5  --  GAP DETECTOR + RESEARCH WIZARD  (week 2-3)
=========================================================

Sean directive 2026-06-01: model intelligence doesn't matter as long as it can TALK and knows how to DO RESEARCH. SearxNG must auto-trigger when knowledge-gap detected. This is the ACTIVE gap-detection layer that libbie never had (libbie did passive context curation only).

Sits at the LLM-OUTPUT boundary, not input. Every LLM response is intercepted, scored, and possibly rewritten with evidence before reaching Sean.

1. Gap detector (swiszcli/gap_detector.py)
   - cheap regex + heuristic scoring on draft LLM output, BEFORE shown to user
   - flag triggers:
     a. hedge words: "i think", "probably", "i'm not sure", "as of my training", "i believe", "might be"
     b. self-contradiction with recalled <recalled_context> block
     c. asserted dates/numbers/version-strings without source citation
     d. claims about external state: prices, latest releases, repo status, current events
     e. proper-noun references not present in swizmem or contexts.db
   - any flag -> response held back, gap query derived from flagged claim (the literal claim becomes the query)
   - FAIL LOUDLY: log every flag with the trigger that fired

2. Research wizard (swiszard handler: research)
   - one job: take a query, fetch evidence, return compact summary
   - sources, fanned out in parallel:
     a. searxng (local, already running) -> top 3 results
     b. swizmem recall -> "have we discussed this before"
     c. contexts.db recall -> "did we work on this recently"
     d. filesystem grep against ~/dev or configured roots -> "is this in my own code"
   - returns: { query, sources: [{url|path, snippet, score}], summary: str }
   - summary is JUST extracted text, no LLM rewrite at this stage (deterministic)

3. Retry loop
   - gap detected -> research wizard runs -> evidence injected as <research_context> block -> LLM retries the answer with full evidence
   - Sean sees the post-research answer only
   - if research returns empty: surface honestly ("i couldn't find evidence for X — proceeding with caveat") instead of fabricating

4. Wizard registry (extensible)
   - research is the first wizard, not the only one
   - future wizards (arxiv, github, package-registry, calendar, etc) register the same way
   - gap-detector dispatches to whichever wizard matches the gap kind

Success criteria P1.5:
- 9b model output never contains unhedged guesses on external state
- searxng fires automatically on dates/versions/external claims, never on routine asks
- response latency stays under 2s on cached gaps, under 5s on fresh searxng fan-outs


WHY THIS IS DIFFERENT FROM LIBBIE
=================================

libbie did PASSIVE context curation: model gets injected context, still has to reason from it. if the context was wrong/missing, model would still produce a hedged answer.

swiszCLI does ACTIVE gap-detection -> wizard-dispatch: model produces a draft, the draft itself gets audited for ignorance, wizards fill the gap, model retries with evidence. the model's epistemic responsibility shrinks to "know when you don't know." everything else is offloaded.

formula: small model + cheap gap detection + fast research wizards = feels omniscient regardless of model size.


PHASE 2  --  PROMOTION + PRUNE  (week 3)
========================================

The memory plan kicks in. chunks -> lessons -> swizmem.

1. Promotion daemon
   - write-triggered: chunk hitting retrievals >= 5 gets promoted
   - one cheap LLM call summarizes to a lesson
   - lands in swizmem as kind=lesson, source=promoted_from_chunk_<id>

2. Gratitude weighting
   - heuristic: thanks/perfect/that worked
   - chunks within prior 3 turns: retrievals++ (quality boost)

3. Prune policy
   - chunks > 30d AND retrievals==0 -> delete
   - tool_results > 7d AND retrievals==0 -> delete
   - session_frames never auto-pruned

4. examples table maintenance
   - examples with losses > wins * 2 AND weight < 0.2 -> archive (soft delete)


PHASE 3  --  FEDERATED SHARING  (week 4+)
=========================================

Opt-in. Pure additive.

1. Export examples to JSON (strip slot values, keep text+target shape+source)
2. Public registry: github cadenKel/swiszcli-examples
3. Import: swiszcli examples pull <user>
4. Imported rows start at weight=0.3, source=github_shared, earn through local wins


PHASE 4  --  REVISIT
====================

After P0-P3, evaluate honestly:
- threshold ladder enough or need probability calibration?
- shorthand resolution need an HMM or is last-2-turns embedding sufficient?
- enough labeled data accumulated to make a tiny seq2seq worthwhile?

Probably none of these are needed. The bar: does it feel like a talking notepad? If yes, stop adding.


THE CORE DISCIPLINE: WIZARD-AS-INTERVIEWER, NAME-ONLY INVOCATION
================================================================

This is the load-bearing pattern of the entire design. Without it, the LLM ends up memorizing tool schemas — which is the exact thing small models fail at and the reason "claude code clones" on 9b models suck.

THE RULE
--------

The LLM only ever emits a wizard NAME. Never args. Never JSON. Never a schema-shaped call.

To do anything beyond conversation, the LLM emits:

  wizard: <name>

That's it. The wizard then takes over and interviews the LLM (or Sean) one question at a time using the existing wizard primitives (pick / text / confirm / multi / nested / action — already shipped per memory:762).


WHAT THE LLM SEES
-----------------

The LLM's entire tool surface is a single tool:

  invoke_wizard(name: str)

The system prompt contains a short list of wizard names + one-line descriptions:

  research      -- fetch evidence from web/swizmem/files
  read          -- read a file
  grep          -- search file contents
  edit          -- modify a file
  shell         -- run a shell command
  remember      -- save a fact to swizmem
  ...

Fits in ~200 tokens. No JSON schema. No arg formatting. No structured output anywhere.


HOW WIZARDS GET THEIR ARGS
--------------------------

Every wizard implements an interview() method. Wizard fills its own form via conversation:

  research wizard:
    Q1 (text): what should i search for?
       LLM answers in prose -> wizard extracts query
    Q2 (pick): which sources? [web | memory | files | all]
       LLM answers in prose -> wizard maps to enum
    Q3 (confirm): proceed? Y/n
       (only shown to Sean for destructive ops; auto-Y for research)

Each question is a tiny generation. Model never sees the full args schema for the wizard. It just answers a question.

For destructive wizards (edit, shell, delete), the interview surfaces to Sean for confirmation. For read-only wizards (research, read, grep), the interview runs against the LLM silently.


WHY THIS WORKS ON A 9B
----------------------

The 9b's job shrinks to two skills it actually has:
  1. Pick a name from a short list (4-bit decision)
  2. Answer a single natural-language question with a sentence

Both are well within reach for a 9b — even a 3b. What 9bs FAIL at is emitting valid JSON matching a 40-line schema. That never happens here.

The schema gets distributed across the wizards themselves. Each wizard only needs to extract its own args from prose, which is a deterministic parser job, not a model job.


EXAMPLES ROUTER UPGRADE
-----------------------

The examples table from the main plan now stores (text, embedding, WIZARD_NAME) instead of (text, embedding, swiszard_DSL_string). The router matches user input to wizard names, not to DSL strings.

  example row: ("show me agent.py", <embedding>, "read", source=seed, weight=1.0)
  example row: ("any TODOs in ~/swiszcli", <embedding>, "grep", source=seed, weight=1.0)

When the examples table matches, the wizard fires directly and interviews from current context (recent turns, recalled chunks) instead of asking the LLM. Even more tokens saved.


WIZARD REGISTRY AS SINGLE SOURCE OF TRUTH
-----------------------------------------

swiszcli/wizards/__init__.py exports REGISTRY: dict[str, Wizard]
Each Wizard has:
  name: str
  description: str  (one line, shown to LLM)
  interview() -> dict  (asks questions, returns filled args)
  execute(args) -> result  (deterministic)
  destructive: bool  (if True, Sean confirms before execute)

Adding a new wizard = adding one entry. LLM picks it up automatically via registry sync into system prompt. No schema churn, no retraining, no prompt-engineering tax.


CONSEQUENCES FOR PHASE PLAN
---------------------------

P0: examples table stores wizard NAMES. Seed phrases per wizard, not per swiszard DSL string. Existing swiszard regex dispatcher stays as-is for power users typing raw DSL.
P1: one-tap learning grows the examples table with (user_phrase -> wizard_name) pairs.
P1.5: gap detector + research wizard already follow this pattern by design.
P2-P4: unchanged, all built on this discipline.

The "minimum schema the LLM memorizes" goes from "all wizard arg signatures" to "list of wizard names." That's the unlock.


CROSS-CUTTING RULES
===================

- Fail loudly: no silent fallbacks. Sub-threshold matches log why.
- Toggle flags: router_enabled, learning_enabled, promotion_enabled.
- Storage: ~/.swiszcli/contexts.db. swizmem stays in its own db.
- Scope: chunks session-scoped, session_frames cross-session, examples global per user.


WHAT GETS BUILT IN WHAT ORDER (one-liner)
=========================================

P0: contexts.db + embed client + examples router with seed phrases + threshold ladder
P1: one-tap learning grows examples table
P2: promotion daemon + prune + gratitude
P3: federated examples
P4: revisit; probably stop


NEXT ACTION
===========

P0 file 1: swiszcli/context_store.py
- create contexts.db
- chunks table (per memory-ideas doc)
- examples table (per above)
- methods: store_chunk, recall_chunks, store_example, match_example

Then wire examples router into agent.py BEFORE the LLM call.


BUILD LOG
=========

P0 SHIPPED 2026-06-01
---------------------
Modules:
  swiszcli/embed.py         (1932b) — nomic-embed-text via ollama, CPU-pinned, 24h keep_alive
  swiszcli/context_store.py (7278b) — sqlite chunks + examples, cosine retrieval, retrievals counter
  swiszcli/router.py        (5579b) — 7 wizards × 5 seed phrases = 35 examples, threshold ladder
  swiszcli/chunks.py        (3513b) — ChunkCapture: window=8 rolling, tool_result, session_frame
  swiszcli/router_hint.py   (2092b) — composes <router_hint confidence=HIGH|MED|LOW> blocks

Wiring (swiszcli/cli.py):
  - boot: instantiate ContextStore at cfg.state_dir/contexts.db, seed router
  - per-turn recall_fn wrapper: queries swiszContext chunks, computes router decision,
    stashes hint + chunks block on state.last_p0_* attrs
  - per-turn combined_renderer wrapper: appends hint + chunks to existing memory block
  - on_tool_end wrapper: records tool_result chunks (deterministic, no LLM)
  - per-turn agent.turn() suffix: records user + assistant turns into ChunkCapture
  - finally block: close_session writes session_frame on clean exit

Tests:
  tests/test_p0_smoke.py       — embed dim 768, chunk in-session, session_frame cross-session,
                                  router accuracy 6/6 on hand-picked paraphrases
  tests/test_p0_integration.py — full end-to-end: seed → 10-turn convo → recall → hint compose
                                  → session_frame → retrieval counter increments

What P0 does NOT do yet:
  - LLM bypass on SILENT mode (would require deterministic arg extractors per wizard)
  - one-tap learning (P1)
  - gap detector / research wizard (P1.5)
  - promotion daemon (P2)

Status: green. Foundation in place. Next: P1.



P1 + P1.5 SHIPPED 2026-06-01 (same session)
-------------------------------------------
P1 (observational learning):
  swiszcli/learn.py — Learner.observe(user_text, swiszard_task, success)
  Pattern-matches the swiszard DSL prefix to infer wizard name, embeds
  user text, stores as learned example (source=learned, weight=1.0).
  Dedup at cosine 0.92: same wizard + above threshold = reinforce in place
  (record_win), no new row.

  Wired in cli.py on_tool_end: every successful swiszard call gives a
  free labeled pair. After ~10-20 turns the table grows enough that
  paraphrases hit SILENT/PREVIEW mode in the router.

  Test: tests/test_p1_learn.py — pre-learn 0.702 -> post-learn 1.000 on
  exact, 0.838 on paraphrase. Dedup verified (36 -> 36 examples on
  re-observe).

P1.5 (gap detector + research wizard):
  swiszcli/gap_detector.py — detect(draft) returns GapVerdict with
  hedge/external/fabrication regex hits + research query seeds.
  swiszcli/research_wizard.py — research(queries, ...) fans out to
  searxng (via swiszard 'search the web for'), swizmem recall, and
  swiszContext chunk recall. Returns deterministic <research_context>
  block.

  Agent.__init__ got optional post_stream_check param. When the final
  draft (no tool calls) has gaps, the closure in cli.py runs the
  research wizard, builds a <gap_detector>+<research_context> block,
  appends it as a user turn, re-streams. One retry max.

  Test: tests/test_p1_5_gap.py — clean draft passes, hedge/external/fab
  drafts flagged correctly, research wizard fan-out verified with stubs.

Status as of end-of-session: P0 + P1 + P1.5 all green. 4 test suites
passing. cli.py imports cleanly. Foundation done. Next phases (P2
promotion, P3 federation) untouched.


P2 + P3 SHIPPED 2026-06-01 (same session)
------------------------------------------
P2 (dream_cycle):
  swiszcli/dream_cycle.py — DreamConfig (TOML) + run(store, ...)
  Promotes chunks with retrievals >= promote_threshold to swizmem as
  kind=lesson. Prunes chunks older than prune_days (session_frames +
  promoted exempt). Deprecates learned examples with bad win/loss ratio
  (seeds never touched). Returns DreamReport, appends JSON to
  ~/.swiszcli/dream_cycle.log.

  CLI: swiszcli-dream [--dry-run] [--json] [--config PATH]
  systemd: contrib/systemd/swiszcli-dream.{service,timer} — 4am daily.

  LIVE TEST: real swizmem write+cleanup verified (mem_id=773 round-trip,
  320ms). test_p2_dream.py exercises dry-run + live + idempotency + log
  file. PASSED.

P3 (federation — local export/import only):
  swiszcli/federation.py — export_examples(store, path) dumps learned
  examples to JSON (text + base64 embedding + wizard + weight + wins +
  losses). import_examples(store, path, trust) loads, dedups by
  (text, wizard), inserts as source=federated with weight * trust.

  CLI: swiszcli-federate export FILE | import FILE [--trust 0.5]

  NO automatic github push. User chooses manually whether to share.
  Respects Sean privacy: opt-in at every step. test_p3_federation.py
  verified roundtrip + trust scaling + dedup. PASSED.

OPS (P2 sibling):
  swiszcli/status_cli.py — swiszcli-status shows contexts.db stats,
  swizmem health, last dream_cycle run.

pyproject console_scripts now:
  swisz / swiszcli       (main agent)
  swiszcli-dream         (P2 dream cycle)
  swiszcli-status        (introspection)
  swiszcli-federate      (P3 export/import)

Status end-of-session: P0 + P1 + P1.5 + P2 + P3 all green. 7 test
suites (5 new + 2 pre-existing untouched). Remaining: P4 honest re-eval
after a few days of real use. github sharing of learned examples =
manual git push when Sean decides.
