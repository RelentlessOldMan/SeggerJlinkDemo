"""Load a directory tree of scripts, parse every file, and resolve the call graph."""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

from .lang import parser
from .lang.parser import ParsedFile, Stmt, call_target, jump_target, walk
from .lang.vocab import Vocab, load

SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv"}
MAX_BYTES = 8 * 1024 * 1024


@dataclass
class Edge:
    src: str                 # caller relative path
    lineno: int
    kind: str                # do | run | pstep | chdir_do | on_do | globalon_do | dodecrypt
    target_text: str
    status: str              # found | found_by_name | ambiguous | missing | t32_system | temp | dynamic
    dst: str = ""            # resolved relative path
    stmt: Stmt | None = None


@dataclass
class FileInfo:
    rel: str
    size: int
    line_endings: str
    pf: ParsedFile | None
    error: str = ""
    edges: list[Edge] = field(default_factory=list)
    gosub_unresolved: int = 0
    gosub_total: int = 0


@dataclass
class Corpus:
    root: pathlib.Path
    files: dict[str, FileInfo]
    skipped: dict[str, int]
    vocab: Vocab
    by_basename: dict[str, list[str]] = field(default_factory=dict)

    def callers(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {k: set() for k in self.files}
        for f in self.files.values():
            for e in f.edges:
                if e.dst:
                    out.setdefault(e.dst, set()).add(f.rel)
        return out


def _line_endings(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    kinds = [k for k, n in (("crlf", crlf), ("lf", lf), ("cr", cr)) if n]
    return "mixed" if len(kinds) > 1 else (kinds[0] if kinds else "none")


def discover(root: pathlib.Path, exts: tuple[str, ...]) -> tuple[list[pathlib.Path], dict[str, int]]:
    found, skipped = [], {"too_large": 0, "unreadable": 0}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.lower().endswith(exts):
                found.append(pathlib.Path(dirpath, fn))
    found.sort()
    return found, skipped


def load_corpus(root: str | os.PathLike, exts=(".cmm",), vocab: Vocab | None = None) -> Corpus:
    root = pathlib.Path(root).resolve()
    v = vocab or load()
    paths, skipped = discover(root, tuple(e.lower() for e in exts))
    files: dict[str, FileInfo] = {}
    for p in paths:
        rel = p.relative_to(root).as_posix()
        try:
            size = p.stat().st_size
            if size > MAX_BYTES:
                skipped["too_large"] += 1
                continue
            data = p.read_bytes()
        except OSError:
            skipped["unreadable"] += 1
            continue
        try:
            pf = parser.parse_bytes(data, v)
            files[rel] = FileInfo(rel, size, _line_endings(data), pf)
        except Exception as exc:  # the scan must never die on one odd file
            files[rel] = FileInfo(rel, size, _line_endings(data), None, error=type(exc).__name__)
    c = Corpus(root, files, skipped, v)
    for rel in files:
        c.by_basename.setdefault(pathlib.PurePosixPath(rel).name.lower(), []).append(rel)
    lower_index = {rel.lower(): rel for rel in files}
    for f in files.values():
        if f.pf:
            _resolve_edges(c, f, lower_index)
    return c


CALL_KINDS = {"DO": "do", "RUN": "run", "PSTEP": "pstep", "ChDir.DO": "chdir_do",
              "DODECRYPT": "dodecrypt", "ON": "on_do", "GLOBALON": "globalon_do"}


def resolve_path(c: Corpus, caller_rel: str, text: str, lower_index: dict[str, str]):
    """Resolve a literal script path. Returns (status, rel)."""
    t = text.strip().strip('"').replace("\\", "/")
    if not t:
        return "missing", ""
    if "&" in t:
        return "dynamic", ""
    caller_dir = pathlib.PurePosixPath(caller_rel).parent
    if t.startswith("~~~~"):
        t = (caller_dir / t[4:].lstrip("/")).as_posix()
    elif t.startswith("~~~"):
        return "temp", ""
    elif t.startswith("~~"):
        return "t32_system", ""
    if not pathlib.PurePosixPath(t).suffix:
        t += ".cmm"
    cands = []
    if not (t.startswith("/") or (len(t) > 1 and t[1] == ":")):
        cands += [(caller_dir / t), pathlib.PurePosixPath(t)]
    for cand in cands:
        norm = os.path.normpath(cand.as_posix()).replace("\\", "/")
        if norm.lower() in lower_index:
            return "found", lower_index[norm.lower()]
    named = c.by_basename.get(pathlib.PurePosixPath(t).name.lower(), [])
    if len(named) == 1:
        return "found_by_name", named[0]
    if len(named) > 1:
        return "ambiguous", ""
    return "missing", ""


def _resolve_edges(c: Corpus, f: FileInfo, lower_index):
    labels = f.pf.labels
    subs = f.pf.subroutines
    for st in walk(f.pf.stmts):
        if st.kind != "command":
            continue
        canon = st.canon
        if canon in CALL_KINDS:
            tgt = call_target(st)
            if tgt and (canon not in ("ON", "GLOBALON") or tgt):
                status, dst = resolve_path(c, f.rel, tgt, lower_index)
                f.edges.append(Edge(f.rel, st.lineno, CALL_KINDS[canon], tgt, status, dst, st))
        jt = jump_target(st)
        if jt and (canon == "GOSUB" or (canon in ("ON", "GLOBALON") and "GOSUB" in [a.upper() for a in st.args])):
            f.gosub_total += 1
            if "&" not in jt and jt not in labels and jt not in subs:
                f.gosub_unresolved += 1


def call_depths(c: Corpus) -> tuple[dict[str, int], int]:
    """Longest DO-chain depth from each file (cycles cut), and the number of cyclic files."""
    memo: dict[str, int] = {}
    on_stack: set[str] = set()
    cyclic: set[str] = set()

    def depth(rel: str) -> int:
        if rel in memo:
            return memo[rel]
        if rel in on_stack:
            cyclic.add(rel)
            return 0
        on_stack.add(rel)
        d = 0
        for e in c.files[rel].edges:
            if e.dst and e.dst in c.files:
                d = max(d, 1 + depth(e.dst))
        on_stack.discard(rel)
        memo[rel] = d
        return d

    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 20000))
    try:
        for rel in c.files:
            depth(rel)
    finally:
        sys.setrecursionlimit(old)
    return memo, len(cyclic)
