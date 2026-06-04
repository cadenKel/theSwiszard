"""swiszcli propose: review and approve synthesized wizard proposals."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

def _store_and_synth():
    from .context_store import ContextStore
    from .tool_synthesis import ToolSynthesizer
    from .embed import embed
    state_dir = Path.home() / ".swiszcli"
    store = ContextStore(db_path=state_dir / "contexts.db")
    synth = ToolSynthesizer(store)
    return store, synth, embed

def cmd_list(args):
    _store, synth, _embed = _store_and_synth()
    pending = synth.list_pending()
    if not pending:
        print("(no pending proposals)")
        return 0
    for f in pending:
        d = json.loads(f.read_text())
        print(f"{d['proposal_id']}  {d['suggested_name']:24s}  ({d['occurrences']}x)  sig: {d['signature']}")
    return 0

def cmd_show(args):
    _store, synth, _embed = _store_and_synth()
    prop = synth.load_proposal(args.proposal_id)
    if not prop:
        print(f"no such proposal: {args.proposal_id}", file=sys.stderr)
        return 2
    print(json.dumps(prop, indent=2))
    return 0

def cmd_approve(args):
    _store, synth, _embed = _store_and_synth()
    result = synth.approve(args.proposal_id, embed_fn=_embed)
    print(json.dumps(result, indent=2))
    return 0

def cmd_reject(args):
    _store, synth, _embed = _store_and_synth()
    result = synth.reject(args.proposal_id)
    print(json.dumps(result, indent=2))
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(prog='swiszcli-propose', description='review wizard proposals')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list').set_defaults(func=cmd_list)
    sp = sub.add_parser('show'); sp.add_argument('proposal_id'); sp.set_defaults(func=cmd_show)
    sa = sub.add_parser('approve'); sa.add_argument('proposal_id'); sa.set_defaults(func=cmd_approve)
    sr = sub.add_parser('reject'); sr.add_argument('proposal_id'); sr.set_defaults(func=cmd_reject)
    args = ap.parse_args(argv)
    return args.func(args)

if __name__ == '__main__':
    sys.exit(main())
