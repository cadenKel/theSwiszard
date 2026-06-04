# Swiszard

Deterministic terminal delegate. No LLM generation — embeddings only.

## What it is

The hands of the mind palace. When swiszCLI needs a file read, a shell command run, an AST transformed, or a memory recalled, it delegates to swiszard. Everything is deterministic — regex-dispatched handlers, no transformer inference.

## DSL

```bash
# File operations
run: <command>                       # shell
read /path/to/file                   # read full file
find *.py in /path                   # glob
grep TEXT in /path                   # content search
write_b64 /path BASE64               # write (base64-safe)
edit /path :: BASE64old :: BASE64new # surgical edit

# AST transforms
ast find FUNC in FILE.py             # locate function
ast wrap FUNC in FILE.py             # wrap in try/except
ast decorate FUNC in FILE.py with @X # add decorator
ast format FILE.py                   # black format

# Multi-step chains
chain: ast wrap foo in x.py | ast format x.py | run: pytest -xq

# Memory (swiszmem)
memory recall <query>                # semantic search
memory remember <fact>               # persistent write
memory status                        # counts + health

# Proactive injection
# Before each handler, the task text is embedded and matched against
# memory triggers. Matched memories are injected into the response.
```

## Mind Palace (swiszproj)

```bash
# Project management substrate inside swiszmem
curl -X POST :7437/project/status -d '{"project":"my-project"}'
# → {"summary": "Project: 8/10 nodes shipped — 1 blocked — frontier: 1 leaf-active"}

# State machine with structural forgetting
curl -X POST :7437/project/transition -d '{"node_id":5,"state":"deprecated"}'
# → Deprecated nodes are physically excluded from ALL retrieval
```

## Architecture

```
server.py → MCP stdio transport
  ├── router.py (embedding + TF-IDF dual-path routing)
  ├── handlers.py (all handler functions)
  ├── proactive_inject.py (memory injection wrapper)
  └── memory_server/ (FastAPI, port 7437)
       ├── app.py (HTTP routes)
       ├── projects.py (mind palace — node tree, state machine, compass)
       ├── embed.py (nomic-embed-text via Ollama)
       ├── embedding_rows.py (multi-vector retrieval)
       ├── code_index.py (file watcher + search)
       └── db.py (SQLite persistence)
```

## Constraints

- **No LLM generation.** Only embeddings for retrieval.
- **No `hermes -z` spawning.** Main-session only.
- **Loud failures.** No silent fallbacks.
- **Deterministic routing.** regex fast-path before TF-IDF.

## Credits

Built by cadenKel (CADEN) + ziggibot-uni (Sean).  
Companion repo: [swiszcli](https://github.com/cadenKel/swiszcli)
