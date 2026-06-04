# Swiszard Memory & Preparers — Architecture Spec

**Status:** Draft v1
**Owner:** Sean Kellogg
**Last updated:** 2026-05-30

---

## 1. Core Insight

The swiszard is a **preparer**. Its purpose is to do slow, expensive work in
the background so the LLM gets instant, dense, high-precision answers when it
asks. Memory, repo indexing, doc freshness, git snapshots, and proactive
context injection are all the same mechanism with different specializations.

> The LLM is the narrator. All retrieval, ranking, and summarization happens
> upstream before tokens hit the context window.

---

## 2. System Topology

```
┌──────────────────────────────────────────────────────────────┐
│                        Hermes Agent                          │
│  ┌────────────────────────────┐                              │
│  │ MemoryProvider plugin      │  (~/.hermes/plugins/swiszmem) │
│  │ - prefetch()               │                              │
│  │ - sync_turn()              │  Thin HTTP adapter only.     │
│  │ - on_session_end()         │  No storage. No logic.       │
│  └────────────────────────────┘                              │
└──────────────┬───────────────────────────────────────────────┘
               │ HTTP (localhost)
               ▼
┌──────────────────────────────────────────────────────────────┐
│              Swiszard Memory Server                          │
│              (FastAPI, ~/swiszard/memory_server/)            │
│                                                              │
│  HTTP API:  /remember  /recall_triggers  /recall_content    │
│             /forget  /prepare  /index_status                 │
│                                                              │
│  Storage:   SQLite + sqlite-vec (or numpy BLOBs)             │
│  Embedding: nomic-embed-text via Ollama                      │
└──────────────┬───────────────────────────────────────────────┘
               ▲                              ▲
               │                              │
┌──────────────┴───────────────┐  ┌──────────┴────────────────┐
│  Swiszard MCP server         │  │  Background daemons       │
│  (swiszard_do actions)       │  │  - auto-indexer (inotify) │
│                              │  │  - doc fresher (cron)     │
│  Same HTTP client as the     │  │  - git snapshotter (hook) │
│  Hermes plugin.              │  │                           │
└──────────────────────────────┘  └───────────────────────────┘
```

**Single source of truth:** the memory server. Hermes and the swiszard MCP are
both clients. Daemons write directly. Nothing else holds memory state.

---

## 3. Data Model

### 3.1 `memories` table

| column        | type     | notes                                       |
| ------------- | -------- | ------------------------------------------- |
| `id`          | INTEGER  | PK                                          |
| `content`     | TEXT     | The actual fact, in natural language        |
| `content_vec` | BLOB     | nomic-embed(content)                        |
| `kind`        | TEXT     | `fact` / `preference` / `decision` / `prep` |
| `session_id`  | TEXT     | Hermes session that wrote this              |
| `turn`        | INTEGER  | Turn number within the session              |
| `timestamp`   | INTEGER  | Unix seconds, UTC                           |
| `source`      | TEXT     | `llm_extracted` / `user_explicit` / `auto_index` / `prep` |
| `ttl_seconds` | INTEGER  | NULL = permanent; otherwise expires         |
| `tags`        | TEXT     | JSON array, optional                        |

### 3.2 `memory_triggers` table

| column         | type    | notes                                              |
| -------------- | ------- | -------------------------------------------------- |
| `id`           | INTEGER | PK                                                 |
| `memory_id`    | INTEGER | FK → memories.id (ON DELETE CASCADE)               |
| `trigger_text` | TEXT    | "when user asks to format python", etc.            |
| `trigger_vec`  | BLOB    | nomic-embed(trigger_text)                          |

One memory ⇒ N triggers. **Triggers are what proactive recall matches on.**
Content vectors are for on-demand search by the LLM.

### 3.3 `repo_files` table (auto-indexer)

| column      | type    | notes                                  |
| ----------- | ------- | -------------------------------------- |
| `id`        | INTEGER | PK                                     |
| `repo_id`   | TEXT    | Derived from path (e.g. `swiszard`)    |
| `path`      | TEXT    | Absolute path                          |
| `mtime`     | INTEGER | File mtime, for staleness checks       |
| `summary`   | TEXT    | First 2KB + filename + path            |
| `vec`       | BLOB    | nomic-embed(summary)                   |

Separate table because file index is huge and shouldn't pollute memory recall.
Queried only when LLM explicitly asks about a repo.

---

## 4. The Two-Vector Memory Pattern

**Problem with standard memory:** embedding the fact and the user query
together rarely works. "Sean prefers tabs over spaces" does not embed close to
"format my python file."

**Solution:** the LLM that writes the memory also writes 2–5 example triggers
— situations where this memory would be needed. Those get embedded separately.

```
Write path:
  user says: "I prefer tabs"
  LLM extracts:
    content: "Sean prefers tabs over spaces in source code"
    triggers:
      - "when formatting code"
      - "when configuring an editor"
      - "when writing python files"
      - "when asked about whitespace style"

  Store: 1 row in memories, 4 rows in memory_triggers

Proactive recall (every turn):
  user_message → embed → cosine match against trigger_vec
  Top-k unique memory_ids → inject content text

On-demand recall (LLM tool call):
  query → embed → cosine match against content_vec
  Direct semantic search of all stored facts
```

**Why this matters:** trigger embeddings are high-precision because they're
*about the situations*, not the facts. The model that wrote them knew the
shape of the queries that would need them.

---

## 5. HTTP API

All endpoints are POST, JSON in/out, served on `127.0.0.1` only.

### 5.1 `/remember`
```json
Request:
{
  "content": "Sean prefers tabs over spaces",
  "triggers": ["when formatting code", "when configuring editor"],
  "kind": "preference",
  "session_id": "abc123",
  "turn": 4,
  "source": "llm_extracted",
  "tags": ["coding", "style"],
  "ttl_seconds": null
}
Response: { "memory_id": 42 }
```

### 5.2 `/recall_triggers` (proactive — used by Hermes prefetch)
```json
Request:  { "query": "format my python file", "top_k": 5 }
Response: {
  "memories": [
    {"id": 42, "content": "...", "trigger_score": 0.87, "provenance": {...}}
  ]
}
```

### 5.3 `/recall_content` (on-demand — used by swiszard_do)
```json
Request:  { "query": "tab preferences", "top_k": 10 }
Response: { "memories": [...] }
```

### 5.4 `/forget`
```json
Request:  { "memory_id": 42 }
Response: { "ok": true }
```

### 5.5 `/prepare` (proactive digging — see §7)
```json
Request:  { "user_message": "fix the bug in pedalmanifest/server.py" }
Response: {
  "prepared": [
    {"kind": "file", "path": "/home/ziggibot/PedalManifest/server.py",
     "content": "...", "freshness_seconds": 300}
  ]
}
```

### 5.6 `/index_status`
Diagnostic. Returns counts, last indexed file, queue depth.

---

## 6. Hermes Plugin (`~/.hermes/plugins/swiszmem/`)

Implements `MemoryProvider` ABC. **Pure HTTP adapter.** No storage, no
extraction logic, no embedding. All of that lives in the server.

| ABC method            | Plugin behavior                                       |
| --------------------- | ----------------------------------------------------- |
| `is_available()`      | HTTP GET `/health`, cached 60s                        |
| `initialize()`        | Stores `session_id`, no-op otherwise                  |
| `prefetch(query)`     | POST `/recall_triggers`, format as injected block     |
| `sync_turn(u, a)`     | Async POST `/remember` after LLM-pass extraction      |
| `on_session_end(msgs)`| Final extraction pass on whole transcript             |
| `get_tool_schemas()`  | Returns `[]` — tools live in swiszard, not here       |

**Extraction pass:** small dedicated LLM call (qwen2.5:7b-caden-hermes via
Ollama, locally) with a strict JSON-out prompt. The same call produces the
triggers. One round-trip per turn, asynchronously, off the critical path.

---

## 7. Proactive Digging (`/prepare`)

Runs at turn start, before Hermes calls the model. Same preparer mechanism,
specialized for context staging instead of memory.

**Pipeline:**
1. Parse the user message for entity hints (regex + simple NER):
   - File paths (anything matching `~/...`, `./...`, `*.py`, etc.)
   - Repo names (anything matching a known indexed `repo_id`)
   - Platform names (stripe, github, vercel, ollama, ...)
2. For each hit, dispatch a deterministic preparer:
   - File hit → read file (truncated)
   - Repo hit → query `repo_files` index, return top files
   - Platform hit → fetch latest docs (cached with TTL)
3. Return the prepared blobs with a hard budget cap (default 2000 tokens).
4. Cache writes go through `/remember` with `kind="prep"` and a TTL.

**Critical:** preparers fail open. If a fetch fails or hits the budget, the
turn proceeds without that context. Never block the LLM.

---

## 8. Auto-Indexer Daemon

Separate process. Writes directly to the same SQLite. Runs as a systemd
user service.

**Loop:**
- `inotifywait -r ~/` with excludes: `node_modules`, `.venv`, `.git`,
  `__pycache__`, `snap/`, `.cache/`, anything > 10MB
- Debounce file events (500ms window)
- For each changed file: enqueue `(path, mtime)`
- Worker pulls from queue, computes summary, calls Ollama embed, upserts into
  `repo_files`

**Resource budget:** single worker, max 2 embed calls/second. Embeddings on
`nomic-embed-text` are ~50ms on CPU. Never starves the main LLM.

---

## 9. Git Snapshotting (Phase 6, deferred)

`swiszard_do git_snapshot <repo>` does:

1. `git add -A`
2. `git commit -m "swiszard snapshot $(date -Iseconds)" --no-verify` on a
   dedicated `swiszard-snapshots` branch
3. `git push origin swiszard-snapshots` using the GitHub token from the
   Hermes agent env (`GITHUB_TOKEN`)
4. Records the commit SHA in memory with `kind="decision"` and a `repo_id` tag

**Rollback:** explicit only. `swiszard_do rollback <commit_sha>` does a
`git checkout -- .` to that snapshot. **No automatic rollback.** The trigger
problem is unsolved and the failure mode (AI nukes in-progress work) is worse
than the failure mode it would prevent.

**Auto-snapshot triggers (later, opt-in per session):**
- After N file writes by Hermes
- At session end
- Before any `swiszard_do` action tagged `destructive`

---

## 10. Provenance Rules (non-negotiable)

Every row in `memories` must have:
- `session_id` (or `"system"` for daemon writes)
- `turn` (or `-1` for non-turn writes)
- `timestamp`
- `source`

Recall responses include provenance. Hermes's injected block formats it as:

```
[memory:42 | session abc123, turn 4, 2026-05-29] Sean prefers tabs over spaces.
```

This makes every injected token traceable. Debuggable. Auditable.

---

## 11. Build Order

1. **Memory server** — FastAPI app, SQLite schema, `/remember` + both recall
   endpoints, Ollama embed client. Standalone, no Hermes integration yet.
2. **Hermes plugin** — `~/.hermes/plugins/swiszmem/`, ABC implementation,
   register in `config.yaml` as `memory.provider: swiszmem`.
3. **Swiszard MCP action** — `swiszard_do memory recall <query>` and
   `swiszard_do memory remember <content>` for on-demand LLM access.
4. **`/prepare` endpoint and turn-start hook** in the Hermes plugin.
5. **Auto-indexer daemon** as a systemd user service.
6. **Git snapshotter** with manual rollback only.

Each phase is independently testable. Each phase delivers value before the
next is built.

---

## 12. Out of Scope (for now)

- Knowledge graph / entity resolution (Hindsight does this; we're choosing
  simplicity over it). Revisit if recall quality is poor.
- Multi-user. Single-user system. `session_id` is for provenance, not
  isolation.
- Cross-machine sync. Local SQLite. If sync becomes needed, replicate the
  whole file.
- Memory editing UI. CLI via swiszard is enough for now.

---

## 13. Success Criteria

- Hermes boots with `memory.provider: swiszmem` and passes `is_available()`.
- A turn that says "I prefer tabs" results in a memory + triggers within 2s
  of the turn ending.
- A subsequent turn asking "format this python file" injects that memory via
  `prefetch` without an explicit LLM tool call.
- The auto-indexer has indexed every file in `~/swiszard`, `~/.hermes`, and
  `~/PedalManifest` within 1 hour of first start.
- Total VRAM impact: zero (everything runs CPU or on the embed model that
  lives outside the main LLM budget).

---

# Addendum v2 — Naming, Deprecation, Supersession, Always-Inject

Status: Draft v2
Last updated: 2026-05-30

## 14. Naming correction: swiszmem -> swiszmem

The plugin and provider name was originally swiszmem. The canonical
contraction of swiszard + memory is swiszmem (preserves the z from
swiszard). Rename touches:

- ~/.hermes/config.yaml line 388: provider: swiszmem
- ~/.hermes/plugins/swiszmem/ (directory rename)
- plugin.yaml name: swiszmem
- Plugin Python module: log namespace, class name, system prompt block
- Skill: hermes-swiszmem-operations

DB and HTTP API are unaffected (server is named swiszard-memory, port 7437).

## 15. Memory lifecycle: deprecation, supersession, always-inject

Three failure modes have surfaced in the two-vector pattern:

1. Stale memories surface in proactive recall. Old facts about retired
   systems (honcho, hindsight, libbie) keep injecting because their trigger
   embeddings still match modern queries. Forgetting loses forensic value.

2. No way to amend a memory in place. When a fact evolves, the LLM has
   to forget+remember and loses the provenance chain.

3. High-priority facts get drowned by similarity. Some facts (user
   communication preferences, hard hardware constraints) should always be
   present in turn context, regardless of query similarity.

### 15.1 Schema additions (additive, non-breaking)

ALTER TABLE memories ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories ADD COLUMN deprecated_reason TEXT;
ALTER TABLE memories ADD COLUMN superseded_by INTEGER REFERENCES memories(id);
ALTER TABLE memories ADD COLUMN lesson TEXT;
CREATE INDEX IF NOT EXISTS idx_memories_deprecated ON memories(deprecated);

Field meanings:
- deprecated: 0 (active) or 1 (excluded from /recall_triggers but still
  visible to /recall_content for forensic search).
- deprecated_reason: human-readable why.
- superseded_by: foreign key to the memory that replaces this one.
  /recall_content follows the chain on display.
- lesson: optional. When supersession happens, the LLM writes what we
  learned from the supersession. Captures evolution of understanding.

### 15.2 New endpoints

POST /deprecate
Request: {"memory_id": 42, "reason": "hindsight retired"}
Response: {"ok": true, "memory_id": 42}

Marks deprecated=1. Memory invisible to /recall_triggers but still
searchable via /recall_content. Triggers remain in DB (no cascade delete)
so forensic recall by trigger still works.

POST /supersede
Request: {
  "old_memory_id": 42,
  "new_content": "...",
  "new_triggers": ["..."],
  "lesson": "...",
  "session_id": "...",
  "turn": 5
}
Response: {"new_memory_id": 712, "old_memory_id": 42}

Flow:
1. Insert new memory with provided content + triggers.
2. Set superseded_by=712 and deprecated=1 on memory 42.
3. Set lesson on memory 42 (lesson lives WITH the deprecated memory,
   so the chain reads: old fact -> lesson learned -> new fact).

### 15.3 always_inject tag

A memory tagged always_inject is returned by /recall_triggers
unconditionally, ahead of similarity matches. Hard cap: at most 5 active
always_inject memories may exist. /pin MUST fail loud with HTTP 409
(pin_limit_reached) when 5 are already pinned; the caller must unpin one before
pinning another. This keeps pinned memory frugal and prevents it from burying
situation-cosine proactive recall. Implementation:

  pinned = first 5 SELECT * FROM memories WHERE always_inject IN tags AND deprecated=0 ORDER BY id
  matched = top-k cosine match on triggers, excluding deprecated and pinned
  return pinned + matched

This is the migration target for the Hermes built-in 2,200-char personal-notes
layer. Tagging always_inject makes a memory equivalent to a pinned note,
but it lives in the same SQLite (single source of truth, infinite capacity,
semantic-searchable).

### 15.4 Swiszard MCP commands

Adds to handler_memory:

  memory deprecate <id> [reason]
  memory supersede <id> with: <new content> [| lesson: <lesson>]
  memory pin <id>            # adds always_inject tag
  memory unpin <id>          # removes always_inject tag
  memory show <id>           # shows full row including supersede chain

### 15.5 Migration plan for existing data

Memories tagged history_migration (from the bulk-import of past sessions)
are mostly raw user messages stored as facts with no real extraction.
One-time audit script:

1. Dump all tags LIKE %history_migration% memories.
2. Heuristic classifier (no LLM needed): drop memories whose content is a
   question, drop memories shorter than 30 chars, drop memories with no
   non-stop-word content. Mark survivors kind=fact and re-trigger them.
3. Forget the dropped ones (real DELETE - they were noise, not history).
4. Manually deprecate any surviving memory that mentions retired systems
   (honcho, hindsight, libbie, lazy_*) with reason: <system> retired.

## 16. Build order for v2

1. Schema migration (ALTER TABLE adds). Idempotent.
2. /deprecate endpoint.
3. /supersede endpoint.
4. /recall_triggers update: pinned + filtered.
5. handler_memory dispatch additions.
6. Skill update (hermes-swiszmem-operations).
7. Plugin/config rename swiszmem -> swiszmem.
8. Migration audit script + execution.

Each step independently testable. Each delivers value alone.
