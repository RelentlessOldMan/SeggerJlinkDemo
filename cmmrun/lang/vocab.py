"""Public PRACTICE / TRACE32 vocabulary and abbreviation matching.

TRACE32 writes each command name with its required letters in upper case
(`SYStem.Up`, `RePeaT`, `OS.PresentPracticeDirectory`). A command may be typed
as any form that keeps every upper-case letter and drops only lower-case ones
(`SYS.U`, `RPT`, `OS.PPD`), case-insensitively. The first letter of a segment
always counts as required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources


def required(seg: str) -> str:
    """Required letters of a canonical segment, upper-cased."""
    req = [c for i, c in enumerate(seg) if i == 0 or c.isupper() or c.isdigit()]
    return "".join(req).upper()


@lru_cache(maxsize=200_000)
def abbrev_ok(tok: str, canon: str) -> bool:
    """True if `tok` is `canon` with only optional (lower-case) letters removed."""
    t, c = tok.upper(), canon
    n = len(t)
    # reach[i]: tok[:i] can be produced by the canon letters seen so far
    reach = [True] + [False] * n
    for j, cj in enumerate(c):
        must = j == 0 or cj.isupper() or cj.isdigit() or not cj.isalpha()
        cu = cj.upper()
        nxt = [False] * (n + 1)
        for i in range(n + 1):
            if not reach[i]:
                continue
            if not must:
                nxt[i] = True                 # drop this optional letter
            if i < n and t[i] == cu:
                nxt[i + 1] = True             # keep it
        reach = nxt
        if not any(reach):
            return False
    return reach[n]


class _Node:
    __slots__ = ("children", "terminal", "_by_first")

    def __init__(self):
        self.children: dict[str, _Node] = {}
        self.terminal = False
        self._by_first: dict[str, list[str]] | None = None

    def by_first(self, ch: str) -> list[str]:
        """Children whose name starts with letter `ch` (every abbreviation keeps the first letter)."""
        if self._by_first is None:
            idx: dict[str, list[str]] = {}
            for k in self.children:
                idx.setdefault(k[:1].upper(), []).append(k)
            self._by_first = idx
        return self._by_first.get(ch, [])


@dataclass(frozen=True)
class Match:
    status: str          # "known" | "partial" | "unknown"
    canon: str           # canonical name, or the known prefix for "partial"
    depth_known: int     # how many segments matched
    ambiguous: bool = False
    abbreviated: bool = False


class Trie:
    def __init__(self, names):
        self.root = _Node()
        for name in names:
            node = self.root
            for seg in name.split("."):
                node = node.children.setdefault(seg, _Node())
            node.terminal = True

    @staticmethod
    def _cands(node: _Node, seg: str):
        """Children matching typed segment `seg`, best first:
        exact > required-letter form > any abbreviation (shorter names first)."""
        u = seg.upper()
        pool = node.by_first(u[:1])
        exact = [k for k in pool if k.upper() == u]
        short = [k for k in pool if required(k) == u and k not in exact]
        rest = sorted((k for k in pool
                       if k not in exact and k not in short and abbrev_ok(seg, k)),
                      key=lambda k: (len(k), k))
        return [(k, False) for k in exact] + [(k, True) for k in short] + [(k, True) for k in rest]

    def match(self, typed: str) -> Match:
        segs = [s for s in typed.split(".")]
        if any(not s for s in segs):
            segs = segs[:segs.index("")] if "" in segs else segs
        best_partial: list = []
        found: list = []

        def dfs(node, i, path, abbr, amb):
            nonlocal best_partial
            if found:
                return
            if i == len(segs):
                if node.terminal:
                    found.append((path, abbr, amb))
                elif len(path) > len(best_partial):
                    best_partial = path
                return
            cands = self._cands(node, segs[i])
            if not cands and len(path) > len(best_partial):
                best_partial = path
            for k, ab in cands:
                dfs(node.children[k], i + 1, path + [k], abbr or ab, amb or len(cands) > 1 and ab)
                if found:
                    return

        dfs(self.root, 0, [], False, False)
        if found:
            path, abbr, amb = found[0]
            return Match("known", ".".join(path), len(path), amb, abbr)
        if best_partial:
            return Match("partial", ".".join(best_partial), len(best_partial))
        return Match("unknown", "", 0)


class Vocab:
    def __init__(self, data: dict):
        self.commands = data["commands"]
        self.functions = data["functions"]
        self.cmd_trie = Trie(self.commands)
        self.func_trie = Trie(self.functions)
        self.words_upper = {w.upper() for w in data["words"]}
        self.words_upper |= {s.upper() for n in self.commands + self.functions for s in n.split(".")}

    @lru_cache(maxsize=100_000)
    def command(self, typed: str) -> Match:
        return self.cmd_trie.match(typed)

    @lru_cache(maxsize=100_000)
    def function(self, typed: str) -> Match:
        return self.func_trie.match(typed)

    def is_word(self, w: str) -> bool:
        return w.upper() in self.words_upper


@lru_cache(maxsize=1)
def load() -> Vocab:
    raw = resources.files("cmmrun.lang").joinpath("vocab_data.json").read_text(encoding="utf-8")
    return Vocab(json.loads(raw))
