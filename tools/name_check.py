"""Guard against committing words that must never appear in this public repo.

The list of forbidden words lives OUTSIDE the repo, one regex per line, at
``~/.cmmrun/private-words.txt`` (override with $CMMRUN_PRIVATE_WORDS). Blank lines
and lines starting with ``#`` are ignored; matching is case-insensitive. Keeping the
list off the repo means the brain can say *that* there are forbidden names without
saying *which*.

It scans every tracked file plus the last commit message, and reports matches with a
masked snippet (the secret itself is never printed). Run it before every commit.

    python tools/name_check.py

Exit codes: 0 clean, 1 one or more hits, 2 no word list on this machine.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys


def word_list_path() -> pathlib.Path:
    env = os.environ.get("CMMRUN_PRIVATE_WORDS")
    return pathlib.Path(env) if env else pathlib.Path.home() / ".cmmrun" / "private-words.txt"


def load_patterns(path: pathlib.Path) -> list[re.Pattern]:
    pats = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            pats.append(re.compile(line, re.IGNORECASE))
        except re.error as e:
            print(f"name_check: bad pattern {line!r}: {e}", file=sys.stderr)
            sys.exit(2)
    return pats


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f]


def last_commit_message() -> str:
    r = subprocess.run(["git", "log", "-1", "--format=%B"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def mask(line: str, m: re.Match) -> str:
    snippet = line.rstrip("\n")
    return (snippet[:m.start()] + "***" + snippet[m.end():]).strip()[:120]


def scan(name: str, text: str, pats: list[re.Pattern], hits: list[str]) -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        for p in pats:
            m = p.search(line)
            if m:
                hits.append(f"  {name}:{lineno}: matches /{p.pattern}/  ->  {mask(line, m)}")


def main() -> int:
    wl = word_list_path()
    if not wl.is_file():
        print(f"name_check: no word list at {wl} on this machine; skipping (exit 2).", file=sys.stderr)
        return 2
    pats = load_patterns(wl)
    if not pats:
        print(f"name_check: word list {wl} is empty; nothing to check.", file=sys.stderr)
        return 2

    hits: list[str] = []
    for f in tracked_files():
        p = pathlib.Path(f)
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:          # skip binaries
            continue
        scan(f, data.decode("utf-8", errors="replace"), pats, hits)
    scan("<last commit message>", last_commit_message(), pats, hits)

    if hits:
        print(f"name_check: {len(hits)} forbidden-name hit(s) against {wl.name}:", file=sys.stderr)
        print("\n".join(hits), file=sys.stderr)
        return 1
    print(f"name_check: clean ({len(pats)} pattern(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
