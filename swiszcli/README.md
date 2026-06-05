# swiszCLI

Multi-paradigm agent CLI. Determined to make local 9B models useful.

## Architecture

```
swz (entrypoint) → swiszcli/cli.py
  ├── swiszard (deterministic delegate — no LLM generation)
  ├── swiszmem (memory server — multi-vector retrieval)
  ├── swiszproj (mind palace — project manager substrate)
  ├── scratchpad (active reasoning buffer)
  ├── wizards (structured multi-step workflows)
  └── Ollama (LLM — qwen3.5:9b-caden-fast)
```

**The thesis:** LLMs are a tool, not the brain. The swiszard handles deterministic work (file ops, shell, AST transforms, memory recall). Wizards handle structured workflows. The mind palace remembers projects across sessions with structural forgetting (deprecated nodes are physically excluded from retrieval). The LLM handles novel situations and creative thinking — called surgically, not as a main loop.

## Components

| Module | Purpose |
|--------|---------|
| `swiszard/` | Deterministic terminal delegate — DSL routing, AST transforms, file ops, chain |
| `swiszmem/` | Memory server with two-vector retrieval (triggers + content), proactive injection |
| `swiszproj/` | Mind palace — typed node tree, state machine, compass status |
| `scratchpad.py` | Active reasoning buffer — what I'm doing right now |
| `wizards_proj.py` | Project wizards — `/proj.status`, `/proj.add_idea`, `/proj.use` |
| `wizards_mem.py` | Memory wizards — `/mem.search`, `/mem.forget` |
| `gap_detector.py` | Automated research pipeline |
| `dream_cycle.py` | Overnight background learning |
| `federation.py` | Cross-instance pattern sharing |
| `router.py` | Intent routing with feedback learning |

## Quickstart

```bash
# Set env
export SWISZCLI_SWISZARD_PATH=/home/ziggibot/theSwiszard/swiszard
# Start swiszmem
systemctl --user start swiszmem.service
# Pull model
ollama pull qwen3.5:9b-caden
# Run
swz
```

## Credits

Built by cadenKel (CADEN) + ziggibot-uni (Sean).  
Companion repo: [swiszard](https://github.com/cadenKel/swiszard)
