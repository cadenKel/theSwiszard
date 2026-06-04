"""AST index + edit engine + undo test."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/ziggibot/swiszcli")
from swiszcli.ast_index import ASTIndex
from swiszcli.edit_engine import EditEngine

print("=" * 60)
print("AST INDEX + EDIT ENGINE TEST")
print("=" * 60)

with tempfile.TemporaryDirectory() as td:
    proj = Path(td) / "src"
    proj.mkdir()
    (proj / "a.py").write_text(
        "def greet(name):\n"
        "    \"\"\"Say hi.\"\"\"\n"
        "    return \"hi \" + name\n"
        "\n"
        "class Calc:\n"
        "    def add(self, x, y):\n"
        "        return x + y\n"
    )
    (proj / "b.py").write_text("def helper():\n    return 42\n")
    
    # --- index ---
    idx = ASTIndex(db_path=Path(td)/"idx.db")
    res = idx.index_project(proj, project_id="testproj")
    print("index:", res)
    assert res["indexed"] == 2
    assert res["errors"] == 0
    
    # find symbol across project
    hits = idx.find_symbol("greet", project_id="testproj")
    assert len(hits) == 1 and hits[0]["kind"] == "function"
    print("found greet at line", hits[0]["lineno"])
    
    cls = idx.find_symbol("Calc", project_id="testproj")
    assert cls and cls[0]["kind"] == "class"
    
    # method has qualname Calc.add
    add = idx.find_symbol("add", project_id="testproj")
    assert add and add[0]["qualname"] == "Calc.add"
    print("qualname:", add[0]["qualname"])
    
    # re-index = unchanged
    res2 = idx.index_project(proj, project_id="testproj")
    assert res2["unchanged"] == 2
    print("re-index:", res2)
    
    # --- edit engine ---
    eng = EditEngine(db_path=Path(td)/"edits.db", snapshot_dir=Path(td)/"snaps")
    
    # Substring replace
    a_path = proj / "a.py"
    prop = eng.propose_replace(str(a_path), "Say hi.", "Greet someone.", description="docstring tweak")
    print(); print(prop.render_preview(20))
    result = eng.apply(prop, session_id="sess1", project_id="testproj")
    print("apply:", result)
    assert result["action"] == "applied"
    assert "Greet someone." in a_path.read_text()
    edit_id = result["id"]
    
    # AST-aware replace: swap whole function body
    new_func = "def greet(name):\n    return f\"hello, {name}!\"\n"
    prop2 = eng.propose_ast_replace(str(a_path), "greet", new_func, description="rewrite greet")
    print(); print(prop2.render_preview(20))
    r2 = eng.apply(prop2, session_id="sess1", project_id="testproj")
    assert r2["action"] == "applied"
    text_now = a_path.read_text()
    assert "hello," in text_now
    assert "Say hi." not in text_now and "Greet someone." not in text_now
    print("ast_replace applied OK")
    
    # History
    hist = eng.history(path=str(a_path))
    print("history:", [(h["id"], h["description"], h["lines_changed"]) for h in hist])
    assert len(hist) == 2
    assert not hist[0]["reverted"]
    
    # Undo most recent (ast_replace)
    u = eng.undo()
    print("undo:", u)
    assert u["action"] == "reverted"
    assert "Greet someone." in a_path.read_text()
    
    # Undo specific (the docstring tweak)
    u2 = eng.undo(edit_id=edit_id)
    print("undo by id:", u2)
    assert u2["action"] == "reverted"
    assert "Say hi." in a_path.read_text()
    
    # Double-undo same id = noop
    u3 = eng.undo(edit_id=edit_id)
    assert u3["action"] == "noop"
    
    # Unique-substring guard
    (proj / "c.py").write_text("x = 1\nx = 1\n")
    try:
        eng.propose_replace(str(proj / "c.py"), "x = 1", "x = 2")
        assert False, "should have raised"
    except ValueError as e:
        print("unique guard OK:", str(e)[:60])
    
    # AST symbol-not-found
    try:
        eng.propose_ast_replace(str(a_path), "nonexistent_fn", "def foo(): pass")
        assert False
    except ValueError as e:
        print("ast not-found OK:", str(e)[:60])
    
    print()
    print("=" * 60)
    print("AST + EDIT ENGINE TEST PASSED")
    print("=" * 60)
