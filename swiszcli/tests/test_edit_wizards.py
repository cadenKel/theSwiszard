"""Edit wizards DSL end-to-end test."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/ziggibot/swiszcli")
from swiszcli.ast_index import ASTIndex
from swiszcli.edit_engine import EditEngine
from swiszcli.edit_wizards import EditOps, dispatch, _dsl_match

print("=" * 60)
print("EDIT WIZARDS DSL TEST")
print("=" * 60)

with tempfile.TemporaryDirectory() as td:
    proj = Path(td) / "p"
    proj.mkdir()
    (proj / "main.py").write_text("def hello():\n    return 1\n")
    idx = ASTIndex(db_path=Path(td)/"idx.db")
    eng = EditEngine(db_path=Path(td)/"edits.db", snapshot_dir=Path(td)/"snaps")
    ops = EditOps(eng, idx, session_id="s1", project_id="p1")
    
    # routing
    assert _dsl_match("edit replace /foo \"a\" with \"b\"")
    assert _dsl_match("find symbol hello")
    assert _dsl_match("index project")
    assert not _dsl_match("read /foo")
    assert not _dsl_match("hello world")
    print("routing OK")
    
    # index
    r = dispatch(ops, "index project " + str(proj))
    print("index:", r)
    assert "indexed:" in r
    
    # find symbol
    r = dispatch(ops, "find symbol hello")
    print(r)
    assert "main.py" in r
    
    # find in file
    r = dispatch(ops, "find symbols in " + str(proj / "main.py"))
    print(r)
    assert "function hello" in r
    
    # propose replace + apply
    main_p = str(proj / "main.py")
    r = dispatch(ops, 'edit replace ' + main_p + ' "return 1" with "return 42" -- bumping return')
    print(r)
    assert "proposal staged" in r
    assert "+    return 42" in r
    r = dispatch(ops, "edit apply")
    print(r)
    assert "applied" in r
    assert "return 42" in (proj / "main.py").read_text()
    
    # AST func rewrite
    new_fn = "def hello():\n    return \"world\"\n"
    r = dispatch(ops, "edit func " + main_p + " hello with:" + chr(10) + new_fn)
    print(r)
    assert "ast proposal staged" in r
    r = dispatch(ops, "edit apply")
    assert "applied" in r
    assert "world" in (proj / "main.py").read_text()
    
    # history
    r = dispatch(ops, "edit history")
    print(r)
    assert "id=" in r
    
    # undo (most recent)
    r = dispatch(ops, "edit undo")
    print(r)
    assert "reverted" in r
    assert "return 42" in (proj / "main.py").read_text()
    assert "world" not in (proj / "main.py").read_text()
    
    # cancel pending
    dispatch(ops, 'edit replace ' + main_p + ' "return 42" with "return 99"')
    r = dispatch(ops, "edit cancel")
    assert "dropped" in r
    assert "return 42" in (proj / "main.py").read_text()  # unchanged
    
    # error path: file missing
    r = dispatch(ops, 'edit replace /nonexistent/x.py "a" with "b"')
    print("missing file:", r)
    assert r.startswith("ERROR:")
    
    # error path: symbol missing
    r = dispatch(ops, "edit func " + main_p + " nope with:" + chr(10) + "def nope(): pass\n")
    print("missing sym:", r)
    assert r.startswith("ERROR:")
    
    # non-DSL passes through
    assert dispatch(ops, "read /tmp/x") is None
    
    print()
    print("=" * 60)
    print("EDIT WIZARDS DSL TEST PASSED")
    print("=" * 60)
