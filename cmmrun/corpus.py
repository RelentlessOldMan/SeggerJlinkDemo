"""Load a directory tree of scripts, parse every file, and resolve the call graph."""
from __future__ import annotations

import fnmatch
import hashlib
import itertools
import os
import pathlib
import queue
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
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
    duplicates: int = 0          # files whose bytes equal an earlier file (parsed once, shared)

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


class Progress:
    """Occasional progress lines on stderr, so a long run over a network share is visibly alive."""

    def __init__(self, enabled: bool = True, every: float = 5.0):
        self.enabled, self.every = enabled, every
        self.t0 = self.last = time.monotonic()
        self.shown = False

    def __call__(self, msg: str, force: bool = False):
        now = time.monotonic()
        if self.enabled and ((force and self.shown) or now - self.last >= self.every):
            self.last, self.shown = now, True
            print(f"cmmrun: [{now - self.t0:6.0f} s] {msg}", file=sys.stderr, flush=True)


def _excluded(rel: str, name: str, patterns: tuple[str, ...]) -> bool:
    low, nlow = rel.lower(), name.lower()
    return any(fnmatch.fnmatchcase(low, p) or fnmatch.fnmatchcase(nlow, p) for p in patterns)


def _list_dir(path: str, rel: str, exts: tuple[str, ...], excludes: tuple[str, ...]):
    """List one directory. Returns (files [(path, rel, size)], subdirs [(path, rel)], counts)."""
    files, subdirs = [], []
    counts = {"too_large": 0, "links": 0, "excluded": 0, "unreadable": 0}
    with os.scandir(path) as it:
        for e in it:
            erel = f"{rel}/{e.name}" if rel else e.name
            try:
                # Never follow links or junctions: they can loop or leave the tree.
                if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                    counts["links"] += 1
                    continue
                if e.is_dir(follow_symlinks=False):
                    if e.name in SKIP_DIRS:
                        continue
                    if excludes and _excluded(erel, e.name, excludes):
                        counts["excluded"] += 1
                        continue
                    subdirs.append((e.path, erel))
                elif e.name.lower().endswith(exts):
                    if excludes and _excluded(erel, e.name, excludes):
                        counts["excluded"] += 1
                        continue
                    # On Windows the size comes from the directory listing: no extra round trip.
                    size = e.stat(follow_symlinks=False).st_size
                    if size > MAX_BYTES:
                        counts["too_large"] += 1
                        continue
                    files.append((e.path, erel, size))
            except OSError:
                counts["unreadable"] += 1
    return files, subdirs, counts


def discover(root: pathlib.Path, exts: tuple[str, ...], excludes: tuple[str, ...] = (),
             jobs: int = 16, progress: "Progress | None" = None):
    """Find script files under root. Directories are listed in parallel, because on a
    network share every listing is a round trip. Returns ([(path, rel, size)], skipped)."""
    progress = progress or Progress(False)
    skipped = {"too_large": 0, "unreadable": 0, "unreadable_dirs": 0, "links": 0, "excluded": 0}
    found: list = []
    ndirs = 0
    done_q: queue.SimpleQueue = queue.SimpleQueue()
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        def submit(path, rel):
            ex.submit(_list_dir, path, rel, exts, excludes).add_done_callback(done_q.put)
        submit(str(root), "")
        outstanding = 1
        while outstanding:
            fut = done_q.get()
            outstanding -= 1
            ndirs += 1
            try:
                files, subdirs, counts = fut.result()
            except OSError:
                skipped["unreadable_dirs"] += 1
                continue
            found.extend(files)
            for k, n in counts.items():
                skipped[k] += n
            for d, drel in subdirs:
                submit(d, drel)
                outstanding += 1
            progress(f"listing: {ndirs} directories, {len(found)} scripts found")
    found.sort(key=lambda f: f[1])
    progress(f"listing done: {ndirs} directories, {len(found)} scripts", force=True)
    return found, skipped


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()      # size already checked from the listing; read(n) would allocate n bytes


def load_corpus(root: str | os.PathLike, exts=(".cmm",), vocab: Vocab | None = None,
                excludes: tuple[str, ...] = (), jobs: int = 16, progress: bool = False) -> Corpus:
    root = pathlib.Path(root).resolve()
    v = vocab or load()
    prog = Progress(progress)
    found, skipped = discover(root, tuple(e.lower() for e in exts),
                              tuple(x.lower().replace("\\", "/") for x in excludes), jobs, prog)
    files: dict[str, FileInfo] = {}
    parsed: dict[bytes, tuple] = {}
    duplicates = 0
    # Read in parallel (I/O bound: overlaps network latency); parse here, in order.
    # At most `window` files are held in memory ahead of the parser.
    window = max(4, 4 * jobs)
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        queue: deque = deque()
        it = iter(found)
        for item in itertools.islice(it, window):
            queue.append((item, ex.submit(_read, item[0])))
        n = 0
        while queue:
            (path, rel, size), fut = queue.popleft()
            nxt = next(it, None)
            if nxt is not None:
                queue.append((nxt, ex.submit(_read, nxt[0])))
            n += 1
            prog(f"reading: {n}/{len(found)} scripts")
            try:
                data = fut.result()
            except OSError:
                skipped["unreadable"] += 1
                continue
            if len(data) > MAX_BYTES:
                skipped["too_large"] += 1
                continue
            # Big trees hold many copies of the same script (branches, releases, archives):
            # parse each distinct content once and share the (read-only) result.
            key = hashlib.blake2b(data, digest_size=16).digest()
            hit = parsed.get(key)
            if hit is None:
                try:
                    hit = (parser.parse_bytes(data, v), _line_endings(data), "")
                except Exception as exc:  # the scan must never die on one odd file
                    hit = (None, _line_endings(data), type(exc).__name__)
                parsed[key] = hit
            else:
                duplicates += 1
            pf, endings, err = hit
            files[rel] = FileInfo(rel, len(data), endings, pf, error=err)
    c = Corpus(root, files, skipped, v, duplicates=duplicates)
    for rel in files:
        c.by_basename.setdefault(rel.rsplit("/", 1)[-1].lower(), []).append(rel)
    lower_index = {rel.lower(): rel for rel in files}
    for f in files.values():
        if f.pf:
            _resolve_edges(c, f, lower_index)
    prog(f"loaded {len(files)} scripts", force=True)
    return c


# Measured on synthetic corpora (README, "Big trees and network shares"): per MiB of *distinct* script text.
EST_MEM_MIB_PER_MIB = 60
EST_SECONDS_PER_MIB = 3.5
EST_MEM_KIB_PER_FILE = 4        # every file, even a copy: its entry, call edges, results
EST_SECONDS_PER_FILE = 0.00025  # on a local disk; a network share adds its latency (hidden by --jobs)


def _hash_file(path: str) -> tuple[bytes, int]:
    with open(path, "rb") as fh:
        data = fh.read()
    return hashlib.blake2b(data, digest_size=16).digest(), len(data)


def inventory(root: str | os.PathLike, exts=(".cmm",), excludes: tuple[str, ...] = (), jobs: int = 16,
              progress: bool = False, top: int = 15) -> dict:
    """Cheap first look at a big tree: list and hash the scripts without parsing them.
    Tells how much distinct script text there is, and estimates what a full scan costs."""
    root = pathlib.Path(root).resolve()
    prog = Progress(progress)
    found, skipped = discover(root, tuple(e.lower() for e in exts),
                              tuple(x.lower().replace("\\", "/") for x in excludes), jobs, prog)
    seen: set[bytes] = set()
    total = distinct = ndistinct = 0
    by_top: dict[str, list[int]] = {}     # top-level dir -> [files, bytes, distinct bytes]
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        for n, ((path, rel, size), res) in enumerate(zip(found, ex.map(_safe_hash, (f[0] for f in found))), 1):
            prog(f"hashing: {n}/{len(found)} scripts")
            if res is None:
                skipped["unreadable"] += 1
                continue
            key, nbytes = res
            top_dir = rel.split("/", 1)[0] if "/" in rel else "."
            row = by_top.setdefault(top_dir, [0, 0, 0])
            row[0] += 1
            row[1] += nbytes
            total += nbytes
            if key not in seen:
                seen.add(key)
                ndistinct += 1
                distinct += nbytes
                row[2] += nbytes
    mib = distinct / 2**20
    biggest = sorted(by_top.items(), key=lambda kv: -kv[1][2])[:top]
    return {
        "files": sum(r[0] for r in by_top.values()), "bytes": total,
        "distinct_files": ndistinct, "distinct_bytes": distinct,
        "skipped": skipped,
        "estimate": {"memory_mib": round(30 + EST_MEM_MIB_PER_MIB * mib + EST_MEM_KIB_PER_FILE * len(found) / 1024),
                     "minutes": round((EST_SECONDS_PER_MIB * mib + EST_SECONDS_PER_FILE * len(found)) / 60, 1)},
        "top_dirs": [{"dir": d, "files": f, "bytes": b, "distinct_bytes": u} for d, (f, b, u) in biggest],
    }


def _safe_hash(path: str):
    try:
        return _hash_file(path)
    except OSError:
        return None


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
