# swiszCLI -> Local Hermes (Memory-Palace) Implementation Plan

> For Caden: master plan. Phases sequential across sessions. Each phase
> has goal, files, bite-sized tasks, verification. Frequent commits.
> Fail loud. No fallbacks. No silent truncation.

Goal: Turn swiszCLI v0.2 into a fully local, 9B-friendly agent that
matches/exceeds Hermes for Seans daily work by giving the LLM a
deterministic memory palace of wizards it can walk AND extend.

Architecture: existing agent loop + sentinel protocol + wizard engine.
We add (in order): multi-vector swizmem, pick_or_new step kind, trace
logger, do_nested correctness + tool-result archive, wizards-as-data,
meta-wizards (wizard.research, wizard.author), then Hermes-parity items
(SOUL.md, auto-catalog, context budgeter, safety gate, session log,
cron, MCP client).

Hard constraints:
- 12GB VRAM, ONE ollama, qwen-class 7-9B target. Embeddings stay CPU.
- 64k hard / 50k soft / 32k rolling. Fail loud at hard cap.
- Swiszard never runs an LLM. Embeddings only.
- No fallbacks. No silent truncation (replace 8000-char cap with
  archive+pointer).
- One concurrent child max. Nested wizards serialize.
- Sentinel <<SWISZ>>...<<END>> stays the ONLY tool surface for the LLM.

================================================================
PHASE 1  multi-vector swizmem (retrieval gets honest)
================================================================
Why first: every other improvement leans on this. Trigger phrases become
first-class retrieval surface instead of being diluted into raw content.

Files:
- modify  ~/swiszard/memory_server/db.py
- modify  ~/swiszard/memory_server/app.py  (recall_triggers endpoint)
- modify  ~/swiszard/memory_server/embed.py  (kind-aware embedder)
- new     ~/swiszard/memory_server/migrations/001_embedding_rows.sql
- test    smoke via swisz: trigger-only lexical hit

Schema:
  CREATE TABLE embedding_rows (
    id          INTEGER PRIMARY KEY,
    memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,   -- raw | trigger | future kinds
    source_id   INTEGER,            -- e.g. triggers.id for kind=trigger
    source_text TEXT    NOT NULL,
    vector      BLOB    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(memory_id, kind, source_id)
  );
  CREATE INDEX idx_embedding_rows_mem ON embedding_rows(memory_id);

Retrieval rule:
  per candidate memory: score = max over rows of weight_k * cos(q, row.vec)
  default weights: trigger=1.00, raw=0.85, (future) intent=0.95
  return matched_kind + matched_source_text for UX.

Tasks:
  1. migration applied idempotently on startup.
  2. backfill: every memory -> raw row; every trigger -> trigger row.
  3. write paths: remember writes raw row; trigger.add writes trigger
     row; trigger.remove CASCADE-deletes its row.
  4. rewrite recall_triggers to query embedding_rows, group by
     memory_id, keep max-weighted score. Legacy triggers.embedding
     column becomes read-only; remove in phase 1.5.
  5. smoke: add memory whose trigger is lexically unrelated to content;
     query for the trigger phrase; assert that memory wins.

Commit: feat(swizmem): multi-vector embeddings (raw + trigger), retrieval by max-kind

================================================================
PHASE 2  pick_or_new step kind + persistent choice pool
================================================================
Why: without this the user/LLM top-N + other -> new pathway loop cannot
close. This is the primitive the memory palace is made of.

Files:
- modify  swiszcli/wizard.py    (Step gets pool field; docstring)
- modify  swiszcli/wizard_ptk.py (add do_pick_or_new)
- new     swiszcli/pools.py     (ChoicePool: sqlite-backed scored pool)
- new     ~/.local/share/swiszcli/pools.db
- test    tests/test_pick_or_new.py (headless ScriptRunner)

Tasks:
  1. ChoicePool(pool_name) with schema
     (id, pool, value, label, use_count, last_used, created_by, created_at).
  2. pool.top(n, ctx) ranks by use_count * recency_decay.
  3. pool.add(value, label, created_by) UPSERT by (pool, value).
  4. pool.touch(value) increments use_count + bumps last_used.
  5. do_pick_or_new: render top-N + a sentinel + new (type one) row.
     On sentinel -> do_text -> pool.add -> return value.
     Always pool.touch on selection.
  6. keep mem-list ChoicesFn as-is (memories arent a pool). Use pools
     for research.sources, wizard.step_kinds, wizard.categories, etc.
  7. ScriptRunner(WizardRunner) for tests: scripts keystrokes, asserts
     pool grows.

Commit: feat(wizard): pick_or_new step kind + sqlite-backed ChoicePool

================================================================
PHASE 3  trace logger (the palaces walls)
================================================================
Files:
- new     swiszcli/trace.py
- new     ~/.local/share/swiszcli/traces.db
- modify  swiszcli/wizard.py  (Wizard.run accepts trace_writer + parent_trace_id)
- modify  swiszcli/cli.py     (instantiate TraceWriter, pass on launches)

Schema:
  CREATE TABLE traces (
    id         TEXT PRIMARY KEY,
    parent_id  TEXT,
    wizard     TEXT NOT NULL,
    source     TEXT NOT NULL,    -- user | llm
    ctx_json   TEXT NOT NULL,
    result_json TEXT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    status     TEXT NOT NULL     -- ok | cancelled | error
  );
  CREATE INDEX idx_traces_wiz ON traces(wizard);
  CREATE INDEX idx_traces_parent ON traces(parent_id);

Tasks:
  1. TraceWriter.start(wizard, source, parent_id) -> trace_id
  2. TraceWriter.end(trace_id, ctx, result, status)
  3. Wizard.run takes optional trace_writer + parent_trace_id; logs.
  4. nested step inherits parent_trace_id = current trace_id.
  5. slash /trace [N] shows last N traces.
  6. slash /replay <id> re-walks with recorded ctx as defaults
     (user can step through and edit).

Commit: feat(trace): persistent wizard trace log + /trace + /replay

================================================================
PHASE 4  fix do_nested + tool-result archive
================================================================
Files:
- modify  swiszcli/wizard_ptk.py (deepcopy ctx into nested; smoke test)
- new     swiszcli/archive.py    (ToolArchive keyed by trace_id+seq)
- modify  swiszcli/protocol.py / swiszcli/agent.py
          (truncate but archive full body; return [archive:ref])

Tasks:
  1. do_nested deepcopies ctx, returns nested result; no parent mutation.
  2. ToolArchive.write(result) -> "[archive:<id>/<n>]".
  3. format_tool_result truncates to 8000 chars AND appends ref.
  4. swiszard handler (or local slash) view <ref> prints full body.
  5. system prompt explains archive refs to the model.

Commit: feat(agent): tool-result archive (no silent loss) + nested ctx safety

================================================================
PHASE 5  wizards-as-data (sqlite-backed REGISTRY)
================================================================
Files:
- new     swiszcli/wizard_store.py (load defs at boot; save new ones)
- schema  wizards(name PK, def_json, source, created_at)
- modify  swiszcli/cli.py boot: wizard_store.load_into_registry()

Tasks:
  1. JSON serializer for Wizard/Step. Lambdas -> named refs in
     swiszcli.callables (WHITELIST). LLM-authored wizards can only
     reference whitelisted callables -> security gate.
  2. wizard_store.save(wiz)  persist + register.
  3. wizard_store.load_into_registry() at boot.
  4. built-in mem.* wizards become seed data (kept as canonical seed in
     wizards_mem.py for backfill).
  5. slashes: /wiz.delete <name>, /wiz.export <name>, /wiz.import <file>.

Commit: feat(wizard): sqlite-persisted REGISTRY; LLM-authored wizards survive restart

================================================================
PHASE 6  meta-wizards (palace grows itself)
================================================================
Files:
- new     swiszcli/wizards_meta.py

wizard.research steps:
  1. text  topic
  2. multi pick_or_new (pool=research.sources)
     seeds: swizmem.recall, trace.search, file.grep, swiszard.command, web.search
  3. action  runs each chosen source, collects snippets
  4. multi  pick snippets to keep as evidence
  5. commit  returns evidence bundle (list[dict])

wizard.author steps:
  1. text  wizard name (dotted)
  2. text  title
  3. text  trigger situation (what fires this wizard?)
  4. nested wizard.research (topic = trigger)
  5. multi pick_or_new (pool=wizard.step_kinds)
  6. loop: per chosen step kind, sub-wizard captures step config
     (key/prompt/pool ref/validator ref/next-rule)
  7. confirm  dry-run preview (renders steps to stdout)
  8. nested wizard.dry_run (optional, headless runner)
  9. confirm  register?
 10. commit  wizard_store.save + emit trigger embeddings (kind=trigger)
     so future invocations of matching situations auto-recall this wizard

Commit: feat(meta): wizard.research + wizard.author; palace grows itself

================================================================
PHASE 7  Hermes-parity items
================================================================
7a SOUL.md baking
   ~/.swisz/soul.md baked into system prompt prepend at boot.
   /soul show, /soul reload. Default: persona + lean-on-wizards
   directive + sentinel protocol reminder.

7b Auto catalog
   At boot, build catalog string: swiszard handlers + registered wizards
   + slash commands. Inject once into system prompt (cap ~2k tokens).

7c Context budgeter
   Replace naive ctx_turns. Budgeter(soft=32k, warn=50k, hard=64k).
   Keeps pinned memories + last N user/assistant + recent archive refs
   (not bodies). FAIL LOUD if a single turn > hard cap.

7d Safety gate
   Wrap swiszard_do with verdict pre-pass for destructive verbs
   (rm, dd, mkfs, drop, > /etc, sudo, curl|sh). If flagged AND not in
   a wizard that explicitly opted in, agent injects confirm step.
   Mirrors swiszards safety: prefix.

7e Session SQLite log
   ~/.local/share/swiszcli/sessions/<id>.db
   tables: messages, calls, traces (FK to traces.db).
   slashes: /sessions, /search, /export.

7f Cron
   Systemd-user wrappers for scheduled wizard runs.
   Entry = (wizard name, schedule, initial ctx).
   Headless runner writes traces like any other run.

7g MCP client
   Native MCP client mirroring Hermes built-in. Servers declared in
   ~/.swisz/config.yaml. Tools become swiszard handlers namespaced
   mcp.<server>.<tool>.

================================================================
Verification ladder (per phase)
================================================================
- pytest under tests/ for each new module
- smoke run via swisz slash + check trace row exists
- real local 9B run: confirm LLM uses the new surface unprompted; if
  not, fix the docs/SOUL/catalog (NEVER a fallback)

================================================================
Build order
================================================================
1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7a -> 7b -> 7c -> 7d -> 7e -> 7f -> 7g
Each phase committed before moving on. Phases 1-6 unlock the dream.
Phase 7 closes the Hermes-parity gap.
