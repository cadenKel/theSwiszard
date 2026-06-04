"""Project state test."""
import sys, tempfile, subprocess, os
from pathlib import Path
sys.path.insert(0, "/home/ziggibot/swiszcli")
from swiszcli.project_state import ProjectStore, detect_project

print("=" * 60)
print("PROJECT STATE TEST")
print("=" * 60)

with tempfile.TemporaryDirectory() as td:
    # Make a fake git project
    proj = Path(td) / "myrepo"
    proj.mkdir()
    subprocess.run(["git", "-C", str(proj), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(proj), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(proj), "config", "user.name", "t"], check=True)
    (proj / "README.md").write_text("hi\n")
    (proj / "main.py").write_text("print(1)\n")
    subprocess.run(["git", "-C", str(proj), "add", "."], check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-q", "-m", "initial"], check=True)
    (proj / "main.py").write_text("print(2)\n")
    subprocess.run(["git", "-C", str(proj), "commit", "-q", "-a", "-m", "fix bug"], check=True)
    # Detect from subdirectory
    sub = proj / "sub"
    sub.mkdir()
    det = detect_project(sub)
    assert det and det["id"] == "myrepo", f"got {det}"
    print("detected:", det)
    # Load full state
    proj_root = Path(td) / "projects_state"
    store = ProjectStore(projects_root=proj_root)
    state = store.load(sub)
    assert state and state.name == "myrepo"
    assert len(state.recent_commits) == 2
    assert "main.py" in state.open_files
    print("commits:", state.recent_commits)
    print("files:", state.open_files)
    # Set goals
    store.set_goals(state.id, "ship the agent\nthen sleep")
    state2 = store.load(sub)
    assert "ship the agent" in state2.goals
    # Record session end
    store.record_session_end(state.id, "sess_abc", turns=12, summary="hacked on dream cycle")
    state3 = store.load(sub)
    assert state3.sessions_count == 1
    assert state3.last_session["turns"] == 12
    print()
    print(state3.render())
    assert state3.id == "myrepo"
    # Not a git repo -> None
    nogit = Path(td) / "nogit"
    nogit.mkdir()
    assert detect_project(nogit) is None
    print()
    print("no-git detection: OK (returned None)")

print()
print("=" * 60)
print("PROJECT STATE TEST PASSED")
print("=" * 60)
