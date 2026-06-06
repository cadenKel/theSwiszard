"""
ast_pin.py — Bridge between swiszcode AST index and swiszproj PM nodes.

Verifies that PM node claims about code structure match the actual indexed AST.
Deterministic — no embeddings, no LLM. Pure module per decision #109.

Architecture (decisions #84, #96, #100, #107):
- Claims stored in pm_node.tags as JSON objects
- verify_claim() queries ast_store for AST match
- pin_verify() writes verification results back to pm_node.tags

Usage:
    from ast_pin import verify_claim, pin_verify, write_claim
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    from ast_store import ASTStore, get_store
except ImportError:
    # When imported from swiszard MCP or other directory
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ast_store import ASTStore, get_store

# Shared memory.db path (swiszmem)
MEMORY_DB = Path.home() / ".hermes" / "swiszard" / "memory.db"



# ── Type name aliases ─────────────────────────────────────────────────────

# Python AST uses ClassDef/FunctionDef, but users say Class/Function.
# Normalize at the verify_claim boundary so both work.
_TYPE_ALIASES = {
    "Class": "ClassDef",
    "Function": "FunctionDef",
    "Method": "FunctionDef",
    "class": "ClassDef",
    "function": "FunctionDef",
    "method": "FunctionDef",
    "def": "FunctionDef",
}

# ── Claim model ────────────────────────────────────────────────────────────

@dataclass
class Claim:
    """A claim that a specific AST node exists at a specific location."""
    file: str           # absolute file path
    node_type: str      # Function, Class, etc.
    node_name: str      # name of the function/class
    expected_lineno: int | None = None  # optional: expected line number
    pm_node_id: int | None = None       # which PM node this claim belongs to
    
    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}
    
    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class VerificationResult:
    claim: Claim
    verified: bool
    evidence: dict = field(default_factory=dict)
    verified_at: float = 0.0
    
    def __post_init__(self):
        if not self.verified_at:
            self.verified_at = time.time()
    
    def to_dict(self) -> dict:
        return {
            "claim": self.claim.to_dict(),
            "verified": self.verified,
            "evidence": self.evidence,
            "verified_at": self.verified_at,
        }


# ── Verification ───────────────────────────────────────────────────────────

def verify_claim(claim: Claim, store: ASTStore | None = None) -> VerificationResult:
    """Verify a single claim against the AST index. Deterministic, no embeddings.
    
    Returns VerificationResult with verified=True if the claimed AST node
    exists in the indexed file.
    """
    if store is None:
        store = get_store()
    
    # Index the file if not already indexed
    store.index_file(claim.file)
    
    # Normalize type names (Class -> ClassDef, Function -> FunctionDef)
    node_type = _TYPE_ALIASES.get(claim.node_type, claim.node_type)
    
    # Query AST store for matching nodes
    matches = store.find_nodes(
        claim.file,
        node_type=node_type,
        name_pattern=claim.node_name,
    )
    
    if not matches:
        return VerificationResult(
            claim=claim,
            verified=False,
            evidence={"error": "not_found", "detail": f"no {claim.node_type} named '{claim.node_name}' found in {claim.file}"},
        )
    
    # If lineno specified, check for exact match
    if claim.expected_lineno is not None:
        lineno_matches = [m for m in matches if m["lineno"] == claim.expected_lineno]
        if not lineno_matches:
            found_lines = [m["lineno"] for m in matches]
            return VerificationResult(
                claim=claim,
                verified=False,
                evidence={
                    "error": "lineno_mismatch",
                    "expected_lineno": claim.expected_lineno,
                    "found_at_linenos": found_lines,
                    "detail": f"found at lines {found_lines}, expected {claim.expected_lineno}",
                },
            )
        match = lineno_matches[0]
    else:
        match = matches[0]
    
    return VerificationResult(
        claim=claim,
        verified=True,
        evidence={
            "found_at": {
                "lineno": match["lineno"],
                "end_lineno": match["end_lineno"],
                "code": match["code"][:200],
            },
        },
    )


# ── PM node integration ────────────────────────────────────────────────────

def _get_pm_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(MEMORY_DB))
    conn.row_factory = sqlite3.Row
    return conn


def read_node_tags(node_id: int) -> list[dict]:
    """Read tags from a pm_node. Returns list of tag objects (strings or dicts)."""
    conn = _get_pm_conn()
    try:
        row = conn.execute("SELECT tags FROM pm_node WHERE id=?", (node_id,)).fetchone()
        if not row:
            raise ValueError(f"pm_node {node_id} not found")
        return json.loads(row["tags"] or "[]")
    finally:
        conn.close()


def write_node_tags(node_id: int, tags: list) -> None:
    """Write tags back to a pm_node."""
    conn = _get_pm_conn()
    try:
        conn.execute(
            "UPDATE pm_node SET tags=?, updated=? WHERE id=?",
            (json.dumps(tags), int(time.time()), node_id),
        )
        conn.commit()
    finally:
        conn.close()


def write_claim(claim: Claim) -> dict:
    """Write a claim to its pm_node's tags. Does NOT verify — use pin_verify() for that.
    
    The claim is stored as a dict in the tags array with a 'claim_type' marker.
    Existing claims for the same PM node are preserved; this appends.
    """
    if claim.pm_node_id is None:
        raise ValueError("claim.pm_node_id is required to write a claim")
    
    tags = read_node_tags(claim.pm_node_id)
    
    # Remove any old claims for this node (replace, don't duplicate)
    tags = [t for t in tags if not (isinstance(t, dict) and t.get("claim_type") == "ast_claim")]
    
    # Add the new claim
    claim_dict = claim.to_dict()
    claim_dict["claim_type"] = "ast_claim"
    tags.append(claim_dict)
    
    write_node_tags(claim.pm_node_id, tags)
    
    return {"ok": True, "node_id": claim.pm_node_id, "claim": claim.to_dict()}


def pin_verify(node_id: int, store: ASTStore | None = None) -> dict:
    """Verify all claims on a pm_node and write results back to tags.
    
    Returns a verification report with all claim results.
    """
    tags = read_node_tags(node_id)
    claims_data = [t for t in tags if isinstance(t, dict) and t.get("claim_type") == "ast_claim"]
    
    if not claims_data:
        return {"node_id": node_id, "claims": [], "summary": "no ast_claims found on this node"}
    
    if store is None:
        store = get_store()
    
    results = []
    for cd in claims_data:
        try:
            claim = Claim.from_dict(cd)
            result = verify_claim(claim, store)
            results.append(result.to_dict())
        except Exception as e:
            results.append({
                "claim": cd,
                "verified": False,
                "evidence": {"error": "parse_failed", "detail": str(e)},
            })
    
    # Store verification results back in tags
    # Remove old verification results, keep non-claim tags
    tags = [t for t in tags if not (isinstance(t, dict) and t.get("claim_type") in ("ast_claim", "ast_verification"))]
    
    # Add claims back with verification status
    for r in results:
        claim_data = r["claim"].copy()
        claim_data["claim_type"] = "ast_claim"
        claim_data["verified"] = r["verified"]  # carry verification into tag
        tags.append(claim_data)
    
    # Add/remove "stale_pin" string tag based on verification outcome
    all_verified = all(r["verified"] for r in results)
    non_claim_tags = [t for t in tags if not (isinstance(t, dict) and t.get("claim_type") == "ast_claim")]
    if not all_verified:
        if "stale_pin" not in non_claim_tags:
            non_claim_tags.append("stale_pin")
    else:
        non_claim_tags = [t for t in non_claim_tags if t != "stale_pin"]
    # Rebuild final tag list: non-claim string tags + updated claim dicts
    final_tags = non_claim_tags + [t for t in tags if isinstance(t, dict) and t.get("claim_type") == "ast_claim"]
    write_node_tags(node_id, final_tags)

    return {
        "node_id": node_id,
        "verified": all_verified,
        "stale": not all_verified,
        "claims": results,
        "summary": "all verified" if all_verified else f"{sum(1 for r in results if r['verified'])}/{len(results)} verified",
    }
