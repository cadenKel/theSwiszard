"""P3 federation test: export -> import roundtrip."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/ziggibot/swiszcli")
from swiszcli.context_store import ContextStore
from swiszcli.federation import export_examples, import_examples

print("=" * 60)
print("P3 FEDERATION TEST")
print("=" * 60)

with tempfile.TemporaryDirectory() as td:
    db_a = Path(td) / "a.db"
    db_b = Path(td) / "b.db"
    sa = ContextStore(db_path=db_a)
    sb = ContextStore(db_path=db_b)

    # A learns 3 examples
    vec = [0.1] * 768
    sa.store_example("open the file", vec, "read", source="learned", weight=1.0)
    sa.store_example("look in there", vec, "grep", source="learned", weight=0.9)
    sa.store_example("hunt down the bug", vec, "grep", source="learned", weight=0.8)
    # And a seed (should not export with learned filter)
    sa.store_example("read this", vec, "read", source="seed", weight=0.5)

    out = Path(td) / "federation.json"
    ex_stats = export_examples(sa, out, source_filter="learned")
    print("export:", ex_stats)
    assert ex_stats.exported == 3
    assert out.exists()

    # B imports
    im_stats = import_examples(sb, out, trust=0.5)
    print("import:", im_stats)
    assert im_stats.imported == 3
    assert im_stats.skipped_dup == 0

    # Verify B has them as federated source
    rows = sb._conn.execute("SELECT text, wizard_name, source, weight FROM examples").fetchall()
    print("B examples:")
    for r in rows:
        print("  ", dict(r))
    assert all(r["source"] == "federated" for r in rows)
    # weight = original * trust
    assert abs(rows[0]["weight"] - 0.5) < 0.01 or abs(rows[0]["weight"] - 0.45) < 0.01 or abs(rows[0]["weight"] - 0.4) < 0.01

    # Re-import should dedup
    im2 = import_examples(sb, out, trust=0.5)
    print("re-import:", im2)
    assert im2.imported == 0
    assert im2.skipped_dup == 3

    sa.close()
    sb.close()

print()
print("=" * 60)
print("P3 FEDERATION TEST PASSED")
print("=" * 60)
