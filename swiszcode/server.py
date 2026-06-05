#!/usr/bin/env python3
"""
swiszcode — repo-level 3D code visualizer.

Two modes:
  /viewer          — repo eagle-eye: all .py files as platonic solids,
                     clustered by structural similarity (embedding cosine),
                     edges = imports between files.
  /viewer?file=X   — single-file hemisphere view: AST tree on upper
                     hemisphere, apex at origin, distance = depth.

Depends on: Ollama nomic-embed-text for similarity clustering.
Deterministic CPU pipeline: AST extract → Ollama embed → numpy layout.
"""

import argparse
import ast
import http.server
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embeddings"
EMBED_ENABLED = True

KEEP = {
    "Module", "FunctionDef", "AsyncFunctionDef", "ClassDef",
    "Import", "ImportFrom",
    "For", "While", "If", "Try", "With",
    "Raise", "Return", "Assert",
    "Assign", "AnnAssign", "AugAssign",
    "Call", "Expr",
}

COLORS = {
    "Module": "#66AAFF", "FunctionDef": "#FF5555", "AsyncFunctionDef": "#FF6688",
    "ClassDef": "#FFAA22", "Import": "#44CC44", "ImportFrom": "#44CC44",
    "For": "#44CCCC", "While": "#44CCCC", "If": "#CCCC44",
    "Try": "#FF8844", "With": "#FF8844", "Raise": "#FF2222", "Return": "#88CC88",
    "Call": "#88CC88", "Expr": "#888888",
    "Assign": "#CC55CC", "AnnAssign": "#CC55CC", "AugAssign": "#CC55CC",
}

ROLE_COLORS = {
    "init": "#888888", "test": "#CCCC44",
    "default": "#CC55CC",
}


# ── Embedding ───────────────────────────────────────────────────────

def embed_texts(texts):
    """Ollama nomic-embed-text. Returns list of float lists."""
    if not EMBED_ENABLED:
        return [None] * len(texts)
    results = []
    for text in texts:
        try:
            data = json.dumps({"model": "nomic-embed-text", "prompt": text}).encode()
            req = urllib.request.Request(OLLAMA_EMBED_URL, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                results.append(json.loads(resp.read()).get("embedding"))
        except Exception:
            results.append(None)
    return results


# ── Cosine Similarity ───────────────────────────────────────────────

def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ── Repo Analysis ───────────────────────────────────────────────────

class ModuleSummary:
    def __init__(self, filepath):
        self.filepath = filepath
        self.functions = []
        self.classes = []
        self.imports = []
        self.imports_from = []
        self.loc = 0

    def extract(self):
        src = Path(self.filepath).read_text(encoding="utf-8")
        self.loc = len(src.splitlines())
        try:
            for node in ast.iter_child_nodes(ast.parse(src, filename=self.filepath)):
                if isinstance(node, ast.FunctionDef):
                    self.functions.append(node.name)
                elif isinstance(node, ast.AsyncFunctionDef):
                    self.functions.append(f"async {node.name}")
                elif isinstance(node, ast.ClassDef):
                    self.classes.append(node.name)
                elif isinstance(node, ast.Import):
                    self.imports.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    self.imports_from.append(node.module or "")
        except SyntaxError:
            pass

    def summary_text(self):
        parts = [f"File: {Path(self.filepath).name}"]
        if self.functions:
            parts.append("Functions: " + ", ".join(self.functions[:15]))
        if self.classes:
            parts.append("Classes: " + ", ".join(self.classes[:10]))
        if self.imports:
            parts.append("Imports: " + ", ".join(self.imports[:10]))
        if self.imports_from:
            parts.append("ImportFrom: " + ", ".join(self.imports_from[:10]))
        parts.append(f"LOC: {self.loc}")
        return " | ".join(parts)


def detect_role(filepath):
    p = Path(filepath)
    if p.stem == "__init__":
        return "init"
    if "test" in p.stem.lower() or "test" in p.parts:
        return "test"
    return "default"


def analyze_repo(root_dir):
    root = Path(root_dir).resolve()
    py_files = [e for e in sorted(root.rglob("*.py"))
                if not any(s in e.parts for s in ("__pycache__", ".venv", "venv", ".venv312", "node_modules", ".git"))
                and not e.name.startswith(".")]

    modules = []
    for pf in py_files:
        s = ModuleSummary(pf)
        s.extract()
        role = detect_role(str(pf))
        modules.append({
            "file": str(pf), "rel_path": str(pf.relative_to(root)),
            "name": pf.name, "stem": pf.stem, "role": role,
            "color": ROLE_COLORS.get(role, ROLE_COLORS["default"]),
            "loc": s.loc, "functions": s.functions, "classes": s.classes,
            "imports": s.imports, "imports_from": s.imports_from,
            "summary": s.summary_text(), "embedding": None,
        })

    # Resolve import targets
    file_map = {m["file"]: m for m in modules}
    for m in modules:
        targets = set()
        for imp in m["imports"]:
            for fpath, other in file_map.items():
                if other["stem"] == imp:
                    targets.add(fpath)
        for imp_f in m["imports_from"]:
            name = imp_f.split(".")[-1] if imp_f else ""
            for fpath, other in file_map.items():
                if other["stem"] == name or fpath.endswith(f"/{name}.py"):
                    targets.add(fpath)
        m["import_targets"] = sorted(targets)

    # Embed
    if EMBED_ENABLED:
        summaries = [m["summary"] for m in modules]
        embeddings = embed_texts(summaries)
        for i, emb in enumerate(embeddings):
            modules[i]["embedding"] = emb

    # Force-directed layout
    positions = force_layout(modules)

    for i, m in enumerate(modules):
        m["pos"] = positions[i] if i < len(positions) else [0, 0, 0]

    return modules


def force_layout(modules, iterations=150):
    n = len(modules)
    if n == 0:
        return []

    positions = []
    for i in range(n):
        phi = math.acos(1 - 2 * (i + 0.5) / n)
        theta = math.pi * (1 + math.sqrt(5)) * i
        r = 6.0
        positions.append([r * math.sin(phi) * math.cos(theta),
                          r * math.sin(phi) * math.sin(theta),
                          r * math.cos(phi)])

    sim = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            s = cosine(modules[i].get("embedding"), modules[j].get("embedding"))
            sim[i][j] = sim[j][i] = s

    import_pairs = set()
    for i, m in enumerate(modules):
        for target in m.get("import_targets", []):
            for j, m2 in enumerate(modules):
                if i != j and m2["file"] == target:
                    import_pairs.add((i, j))

    dt = 0.1
    for _ in range(iterations):
        forces = [[0.0, 0.0, 0.0] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                dx, dy, dz = positions[i][0] - positions[j][0], positions[i][1] - positions[j][1], positions[i][2] - positions[j][2]
                dist = math.sqrt(dx * dx + dy * dy + dz * dz) + 0.01
                fr = 15.0 / (dist * dist)
                fx, fy, fz = dx / dist * fr, dy / dist * fr, dz / dist * fr
                s = sim[i][j]
                if s > 0.4:
                    fa = s * dist * 0.3
                    fx -= dx / dist * fa
                    fy -= dy / dist * fa
                    fz -= dz / dist * fa
                if (i, j) in import_pairs or (j, i) in import_pairs:
                    fi = dist * 0.5
                    fx -= dx / dist * fi
                    fy -= dy / dist * fi
                    fz -= dz / dist * fi
                forces[i][0] += fx; forces[i][1] += fy; forces[i][2] += fz
                forces[j][0] -= fx; forces[j][1] -= fy; forces[j][2] -= fz

        for i in range(n):
            positions[i][0] += forces[i][0] * dt
            positions[i][1] += forces[i][1] * dt
            positions[i][2] += forces[i][2] * dt
        dt *= 0.92

    all_c = [c for p in positions for c in p]
    mx = max(abs(c) for c in all_c) or 1.0
    sc = 8.0 / mx
    for p in positions:
        p[0] *= sc; p[1] *= sc; p[2] *= sc

    return positions


# ── Single-File AST Extraction ──────────────────────────────────────

class FullASTDumper(ast.NodeVisitor):
    def __init__(self, lines):
        self.lines = lines
        self.nodes = []
        self.edges = []
        self.stack = []
        self.seen = set()
        self.nid = 0

    def visit(self, node):
        if id(node) in self.seen:
            return
        self.seen.add(id(node))
        tname = type(node).__name__
        if tname not in KEEP:
            super().generic_visit(node)
            return
        idx = self.nid; self.nid += 1
        name = tname
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Import):
            name = "import " + ",".join(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = f"from {node.module or '.'} import ..."
        depth = len([x for x in self.stack if x is not None])
        size = 0.15 + (0.8 / max(1, depth**0.5))
        code = ""
        ln = getattr(node, "lineno", 0)
        eln = getattr(node, "end_lineno", 0)
        if ln and eln and self.lines:
            s, e = ln - 1, min(eln, len(self.lines))
            if 0 <= s < e:
                raw = "".join(self.lines[s:e]).strip()
                code = raw[:500] + ("..." if len(raw) > 500 else "")
        nd = {"id": idx, "name": name, "type": tname,
              "color": COLORS.get(tname, "#8888FF"),
              "size": size, "depth": depth,
              "lineno": ln, "end_lineno": eln, "code": code}
        self.nodes.append(nd)
        parent = None
        for p in reversed(self.stack):
            if p is not None:
                parent = p; break
        if parent is not None:
            self.edges.append({"from": parent, "to": idx})
        self.stack.append(idx)
        super().generic_visit(node)
        self.stack.pop()


def extract_ast(filepath):
    p = Path(filepath)
    src = p.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src, filename=str(p))
    dumper = FullASTDumper(lines)
    dumper.visit(tree)
    return {"file": str(p), "nodes": dumper.nodes, "edges": dumper.edges}


def hemispherical_layout(nodes):
    """Apex at origin. Children on upper hemisphere (Y>=0). Distance = depth."""
    n = len(nodes)
    if n == 0:
        return []
    max_depth = max((nd.get("depth", 1) for nd in nodes), default=1)
    positions = [[0.0, 0.0, 0.0]]
    for i in range(1, n):
        d = nodes[i].get("depth", 1)
        r = (d / max_depth) * 10.0
        phi = (math.pi / 2) * (i / max(n - 1, 1))
        phi = max(0.15, min(phi, math.pi / 2 - 0.1))
        theta = math.pi * (3 - math.sqrt(5)) * i
        x = r * math.sin(phi) * math.cos(theta)
        y = r * math.cos(phi)
        z = r * math.sin(phi) * math.sin(theta)
        positions.append([x, abs(y), z])
    return positions


# ── HTML (from external files) ──────────────────────────────────────

VIEWER_HTML = open(Path(__file__).parent / "viewer.html").read()
INDEX_HTML_TEMPLATE = open(Path(__file__).parent / "index.html").read()


def render_index(modules, root_label):
    role_counts = defaultdict(int)
    total_loc = 0
    for m in modules:
        role_counts[m["role"]] += 1
        total_loc += m["loc"]
    stats = f"<p>{len(modules)} modules, {total_loc} LOC</p>"
    for role, count in sorted(role_counts.items()):
        c = ROLE_COLORS.get(role, "#888")
        stats += f'<span style="display:inline-block;margin:2px 8px;padding:2px 8px;border-radius:4px;background:{c}22;border:1px solid {c}55;color:{c};font-size:12px">{role}: {count}</span>'

    files_html = ""
    for m in modules[:80]:
        qf = urllib.parse.quote(m["file"], safe="")
        files_html += f'<a href="/viewer?file={qf}" style="display:block;color:#ccc;text-decoration:none;padding:1px 8px;border-radius:3px;font-size:12px" onmouseover="this.style.background=\'rgba(102,170,255,0.1)\'" onmouseout="this.style.background=\'\'">{m["rel_path"]}</a>\n'

    return INDEX_HTML_TEMPLATE.replace("__STATS__", stats).replace("__FILES__", files_html).replace("__ROOT__", root_label)


# ── HTTP Handler ────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    root_dir = "."
    _cache = None
    _cache_time = 0

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                self._index()
            elif path == "/viewer":
                self._viewer(qs)
            elif path == "/repo":
                self._repo_data()
            elif path == "/view":
                self._file_data(qs)
            else:
                self._send(404, "Not found")
        except Exception as e:
            self._send(500, str(e))

    def _index(self):
        ms = self._modules()
        html = render_index(ms, Path(self.root_dir).resolve().name)
        self._html(html)

    def _viewer(self, qs):
        self._html(VIEWER_HTML)

    def _repo_data(self):
        ms = self._modules()
        out = [{"file": m["file"], "rel_path": m["rel_path"], "name": m["name"],
                "stem": m["stem"], "role": m["role"], "color": m["color"],
                "loc": m["loc"], "functions": m["functions"][:20],
                "classes": m["classes"][:10], "imports": m["imports"][:15],
                "import_targets": m["import_targets"], "pos": m["pos"]} for m in ms]
        self._json({"modules": out, "root": self.root_dir})

    def _file_data(self, qs):
        file = qs.get("file", [None])[0]
        if not file:
            return self._json({"error": "missing ?file="})
        data = extract_ast(file)
        positions = hemispherical_layout(data["nodes"])
        for i, nd in enumerate(data["nodes"]):
            nd["pos"] = positions[i] if i < len(positions) else [0, 0, 0]
        self._json(data)

    def _modules(self):
        now = time.time()
        if self._cache and (now - self._cache_time) < 60:
            return self._cache
        self._cache = analyze_repo(self.root_dir)
        self._cache_time = now
        return self._cache

    def _html(self, html):
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        data = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send(self, code, msg):
        data = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {args[0]}\n")


def main():
    p = argparse.ArgumentParser(description="swiszcode")
    p.add_argument("--dir", default=".")
    p.add_argument("--port", type=int, default=8910)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-embed", action="store_true")
    args = p.parse_args()
    if args.no_embed:
        global EMBED_ENABLED; EMBED_ENABLED = False
    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr); sys.exit(1)
    Handler.root_dir = str(root)
    print(f"Analyzing {root}...")
    ms = analyze_repo(str(root))
    embedded = sum(1 for m in ms if m["embedding"])
    print(f"  {len(ms)} modules, {embedded} embedded")
    print(f"swiszcode → http://{args.host}:{args.port}")
    srv = http.server.HTTPServer((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\ndone"); srv.server_close()


if __name__ == "__main__":
    main()
