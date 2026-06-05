# SwiszNet Foundations — Consolidation, Measurement, and Backup

**Goal:** Kill duplication in the wizard registry, prove the learning pipeline works via instrumentation, set up daily GitHub backup so nothing is lost.

**Architecture:** Three independent workstreams. (A) Eliminate the swiszproj/wizards.py stub duplication — those thin shadow dataclasses exist only to break a circular import. Move the actual wizard definitions into swiszcli where the real REGISTRY lives, leave only ProjectClient calls in swiszproj. (B) Add stats reporting so we can measure whether the speculative cache, edge weights, and void filler actually work. (C) Daily git backup via systemd timer — commit + push the monorepo every night.

**Tech Stack:** Python 3.12, systemd user timers, git, sqlite (existing contexts.db, memory.db, wizards.db)

---

## Pre-flight: Commit current state

### Task 1: Commit uncommitted work

**Files:** All currently modified/untracked files in ~/theSwiszard

Step 1: cd /home/ziggibot/theSwiszard && git status --short
Step 2: git add -A
Step 3: git commit -m "chore: working state before swisznet consolidation"
Step 4: git push origin main

---

## Workstream A: Kill Wizard Registry Duplication

**Problem:** swiszproj/swiszproj/wizards.py (234 lines) defines its own Choice/Step/Wizard/register stubs because swiszproj cannot import from swiszcli (circular dependency). The actual wizard definitions (proj.add_idea, proj.use, proj.new, proj.conflicts) live in swiszproj alongside the stubs.

**Solution:** Extract stub types to swiszproj/types.py. Move wizard definitions to swiszcli/wizards_proj.py using the REAL Wizard class. swiszproj/wizards.py becomes a thin module (~60 lines) with only ProjectClient-dependent functions.

### Task A1: Extract stub types to shared module

Create: swiszproj/swiszproj/types.py
Modify: swiszproj/swiszproj/wizards.py

The types.py holds Choice, Step, Wizard, register stubs (10 lines). wizards.py imports them instead of inline definitions.

### Task A2: Move wizard definitions to swiszcli

Modify: swiszcli/swiszcli/wizards_proj.py (create if missing, append if exists)
Modify: swiszproj/swiszproj/wizards.py (remove wizard defs, keep only ProjectClient functions)
Modify: swiszcli/swiszcli/cli.py (update imports)

The proj.add_idea, proj.use, proj.new, proj.conflicts wizard definitions move to swiszcli/wizards_proj.py using the real Wizard class. swiszproj/wizards.py keeps ACTIVE state, _project_choices, _conflict_choices.

cli.py import changes from 'from swiszproj import wizards as wizards_proj' to 'from . import wizards_proj' plus 'from swiszproj.wizards import get_active, set_active'.

---

## Workstream B: Measurement — Instrument the Learning Pipeline

### Task B1: Create stats module

Create: swiszcli/swiszcli/stats.py

A Stats dataclass with counters: spec_hits, spec_misses, spec_attempts, edge_updates, edge_auto_routes, voids_detected, voids_filled, sequences_learned, sequence_hits, examples_learned, examples_reinforced, sources_used, sources_ignored, turns. Thread-safe via threading.Lock. Persists to ~/.swiszcli/stats.json. Exports incr(field, amount=1), snapshot(), report().

### Task B2: Wire stats into cli.py

Modify: swiszcli/swiszcli/cli.py

Add import: from .stats import incr as _stats_incr

Add stats calls at:
- Spec cache hit (after state._spec_hits += 1): _stats_incr('spec_hits')
- Spec cache miss: _stats_incr('spec_misses')
- Cache prime attempt: _stats_incr('spec_attempts')
- Void detected: _stats_incr('voids_detected')
- Void filled: _stats_incr('voids_filled')
- Sequence learned (in _flush_sequence): _stats_incr('sequences_learned')
- Sequence hint injected: _stats_incr('sequence_hits')
- Learner observe: _stats_incr('examples_learned') or _stats_incr('examples_reinforced')
- Turn start: _stats_incr('turns')

### Task B3: Add /stats slash command

Modify: swiszcli/swiszcli/cli.py

In the slash command dispatcher, add a handler for 'stats' that calls from .stats import report and prints it.

---

## Workstream C: Daily GitHub Backup

### Task C1: Create backup script

Create: /home/ziggibot/theSwiszard/scripts/daily-backup.sh

Script: set -e, cd to repo, git add -A, commit if dirty (git diff --cached --quiet check), push. Logs to ~/.swiszcli/backup.log.

Make executable: chmod +x /home/ziggibot/theSwiszard/scripts/daily-backup.sh

### Task C2: Create systemd timer

Create: ~/.config/systemd/user/swiszbackup.service (Type=oneshot, ExecStart=/bin/bash <script>)
Create: ~/.config/systemd/user/swiszbackup.timer (OnCalendar=daily, Persistent=true)

Enable: systemctl --user daemon-reload && systemctl --user enable --now swiszbackup.timer

### Task C3: Fix stale crontab

The existing crontab prune entry points to ~/swiszard which no longer exists.
Fix: crontab -l | sed 's|/home/ziggibot/swiszard|/home/ziggibot/theSwiszard/swiszard|g' | crontab -

---

## Verification Checklist

- Working tree clean in theSwiszard monorepo
- swiszproj/swiszproj/wizards.py under 80 lines
- All proj wizard imports work from swiszcli
- /stats command prints non-zero counters after a session
- systemctl --user list-timers swiszbackup.timer shows active
- systemctl --user start swiszbackup.service exits clean
- crontab -l shows updated prune path

## Pitfalls

- DO NOT delete swiszproj/wizards.py entirely — cli.py imports get_active() from it
- DO NOT change the git remote URL — the token is embedded
- The backup script commits ALL uncommitted work — keep the tree clean or accept auto-commits
- systemd user timers require lingering (loginctl enable-linger) if the user session ends
