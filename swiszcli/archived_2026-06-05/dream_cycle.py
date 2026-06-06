"""P2 dream_cycle: nightly maintenance for swiszContext.

Runs at 4am via systemd timer (or swiszcli dream CLI). Promotes
frequently-recalled chunks into swizmem as lessons, prunes stale chunks,
and deprecates learned examples with bad win/loss ratio.

NO LLM use. Pure deterministic data movement.

Config knobs (tunable via TOML at ~/.swiszcli/dream_cycle.toml):
  promote_threshold (int, default 5)
  prune_days        (int, default 30)
  dep_min_losses    (int, default 3)
  dep_loss_ratio    (float, default 2.0)
  swizmem_url       (str, default http://127.0.0.1:7437)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".swiszcli" / "dream_cycle.toml"
DEFAULT_LOG_PATH = Path.home() / ".swiszcli" / "dream_cycle.log"


@dataclass
class DreamConfig:
    promote_threshold: int = 5
    prune_days: int = 30
    dep_min_losses: int = 3
    dep_loss_ratio: float = 2.0
    swizmem_url: str = "http://127.0.0.1:7437"

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not path.exists():
            return cls()
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        data = tomllib.loads(path.read_text())
        return cls(
            promote_threshold=int(data.get("promote_threshold", 5)),
            prune_days=int(data.get("prune_days", 30)),
            dep_min_losses=int(data.get("dep_min_losses", 3)),
            dep_loss_ratio=float(data.get("dep_loss_ratio", 2.0)),
            swizmem_url=str(data.get("swizmem_url", "http://127.0.0.1:7437")),
        )


@dataclass
class DreamReport:
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    promoted: list = field(default_factory=list)
    pruned_count: int = 0
    deprecated: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def summary(self):
        dt = self.finished_at - self.started_at if self.finished_at else 0
        return {
            "elapsed_s": round(dt, 2),
            "promoted": len(self.promoted),
            "pruned": self.pruned_count,
            "deprecated": len(self.deprecated),
            "proposals": len(self.proposals),
            "errors": len(self.errors),
        }


def run(store, *, config=None, mem_client=None, log_path=None, dry_run=False):
    cfg = config or DreamConfig.load()
    report = DreamReport()

    if mem_client is None and not dry_run:
        from .memory import MemoryClient
        mem_client = MemoryClient(cfg.swizmem_url, session_id="dream_cycle")

    # 1. PROMOTE
    try:
        promotable = store.list_promotable_chunks(min_retrievals=cfg.promote_threshold)
        for ch in promotable:
            content = _format_lesson(ch)
            if dry_run:
                report.promoted.append({"chunk_id": ch["id"], "memory_id": -1,
                    "retrievals": ch["retrievals"], "kind": ch["kind"], "dry": True})
                continue
            try:
                resp = mem_client.remember(content=content, kind="lesson",
                    source="dream_cycle",
                    tags=["swiszcli", "promoted", "from-" + ch["kind"]])
                mem_id = resp.get("id") or resp.get("memory_id") or 1
                store.mark_promoted(ch["id"], mem_id)
                report.promoted.append({"chunk_id": ch["id"], "memory_id": mem_id,
                    "retrievals": ch["retrievals"], "kind": ch["kind"]})
            except Exception as e:
                report.errors.append({"phase": "promote", "chunk_id": ch["id"], "error": str(e)})
    except Exception as e:
        report.errors.append({"phase": "promote_list", "error": str(e)})

    # 2. PRUNE
    try:
        if dry_run:
            cutoff = time.time() - cfg.prune_days * 86400
            cnt = store._conn.execute(
                "SELECT COUNT(*) c FROM chunks WHERE ts < ? AND promoted = 0 AND kind != ?",
                (cutoff, "session_frame")).fetchone()["c"]
            report.pruned_count = cnt
        else:
            report.pruned_count = store.prune_old_chunks(cfg.prune_days)
    except Exception as e:
        report.errors.append({"phase": "prune", "error": str(e)})

    # 3. DEPRECATE EXAMPLES
    try:
        bad = store.list_deprecatable_examples(min_losses=cfg.dep_min_losses,
            loss_ratio=cfg.dep_loss_ratio)
        for ex in bad:
            if dry_run:
                report.deprecated.append({"example_id": ex["id"],
                    "wizard": ex["wizard_name"], "wins": ex["wins"],
                    "losses": ex["losses"], "dry": True})
                continue
            ok = store.deprecate_example(ex["id"])
            if ok:
                report.deprecated.append({"example_id": ex["id"],
                    "wizard": ex["wizard_name"], "wins": ex["wins"],
                    "losses": ex["losses"]})
    except Exception as e:
        report.errors.append({"phase": "deprecate", "error": str(e)})

    # 4. TOOL SYNTHESIS (cluster shell_fallback chunks into wizard proposals)
    try:
        from .tool_synthesis import ToolSynthesizer
        synth = ToolSynthesizer(store, min_cluster_size=cfg.synth_min_cluster)
        proposals = synth.synthesize(dry_run=dry_run)
        for prop in proposals:
            report.proposals.append({
                "proposal_id": prop.proposal_id,
                "suggested_name": prop.suggested_name,
                "signature": prop.signature,
                "occurrences": prop.occurrences,
            })
    except Exception as e:
        report.errors.append({"phase": "synthesis", "error": str(e)})

    report.finished_at = time.time()

    # 5. LOG
    lp = Path(log_path) if log_path else DEFAULT_LOG_PATH
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a") as f:
            f.write(json.dumps({"ts": report.finished_at,
                "summary": report.summary(),
                "promoted": report.promoted[:20],
                "pruned": report.pruned_count,
                "deprecated": report.deprecated[:20],
                "proposals": report.proposals,
                "errors": report.errors,
                "dry_run": dry_run}) + chr(10))
    except Exception:
        pass

    return report


def _format_lesson(chunk):
    kind = chunk["kind"]
    text = chunk["text"]
    retr = chunk["retrievals"]
    sess = chunk["session_id"]
    if kind == "session_frame":
        prefix = "[lesson from session_frame retrieved " + str(retr) + "x] session " + sess[:8] + " arc:"
    elif kind == "tool_result":
        prefix = "[lesson from tool_result retrieved " + str(retr) + "x] recurring tool pattern:"
    else:
        prefix = "[lesson from chunk_window retrieved " + str(retr) + "x] recurring discussion:"
    body = text.strip()
    if len(body) > 1800:
        body = body[:1800] + " ...[truncated]"
    return prefix + " " + body

