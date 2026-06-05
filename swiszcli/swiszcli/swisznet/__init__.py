"""
swisznet — Wizard-to-wizard routing network with learned edge weights.

Thin redirect to swiszcli/chain_credit.py. Uses trace.py TraceWriter
(SQLite, parent_id chaining) as the trace source for chain replay.

Goal: mitigate cloud LLM use by offloading routing to deterministic,
traceable, local-first mechanisms that learn over time. Single 12GB GPU.
90's AI brought back — symbolic, auditable, small-model-compatible.
"""

import sys, os

_monorepo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_swiszcli_path = os.path.join(_monorepo, 'swiszcli')
if _swiszcli_path not in sys.path:
    sys.path.insert(0, _swiszcli_path)

from swiszcli.chain_credit import (
    get_edge_weight,
    record_edge_correct,
    record_edge_incorrect,
    should_auto_route,
    replay_trace_chain,
    format_assignment_report,
)

__all__ = [
    'get_edge_weight', 'record_edge_correct', 'record_edge_incorrect',
    'should_auto_route', 'replay_trace_chain', 'format_assignment_report',
]
