"""Low-level PRACTICE text handling: decoding, logical lines, comments, tokens.

Everything here is lenient: it never raises on bad input. Problems are
recorded as issue codes so `scan` can count them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MACRO_RE = re.compile(r"&\(([A-Za-z_][A-Za-z0-9_]*)\)|&([A-Za-z_][A-Za-z0-9_]*)")
LABEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?!:)")
DEVICE_PREFIX_RE = re.compile(r"^[A-Za-z]::")


@dataclass
class Decoded:
    text: str
    encoding: str
    bom: bool = False
    issues: list[str] = field(default_factory=list)


def decode(data: bytes) -> Decoded:
    """Decode script bytes. TRACE32 scripts are usually ASCII, Latin-1/CP1252 or UTF-8."""
    issues = []
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return Decoded(data.decode("utf-16"), "utf-16", True)
        except UnicodeDecodeError:
            pass
    bom = data.startswith(b"\xef\xbb\xbf")
    if bom:
        data = data[3:]
    if b"\x00" in data:
        issues.append("binary_or_nul_bytes")
        return Decoded(data.replace(b"\x00", b"").decode("latin-1"), "binary?", bom, issues)
    if bom:
        return Decoded(data.decode("utf-8", errors="replace"), "utf-8", True)
    try:
        text = data.decode("ascii")
        return Decoded(text, "ascii")
    except UnicodeDecodeError:
        pass
    try:
        return Decoded(data.decode("utf-8"), "utf-8")
    except UnicodeDecodeError:
        return Decoded(data.decode("cp1252", errors="replace"), "cp1252")


@dataclass
class LogicalLine:
    text: str          # joined text, original spacing kept, no line terminator
    lineno: int        # first physical line (1-based)
    nphys: int         # number of physical lines joined


def logical_lines(text: str) -> list[LogicalLine]:
    """Join lines ending in a backslash (no whitespace after it) with the next line."""
    phys = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[LogicalLine] = []
    i = 0
    while i < len(phys):
        start = i
        cur = phys[i]
        while cur.endswith("\\") and i + 1 < len(phys):
            i += 1
            cur = cur[:-1] + phys[i].lstrip()
        out.append(LogicalLine(cur, start + 1, i - start + 1))
        i += 1
    return out


def strip_comment(s: str) -> tuple[str, str, bool]:
    """Split code from a trailing comment. Returns (code, comment, unterminated_string).

    `;` starts a comment anywhere outside a string. `//` starts one only at the
    beginning of a token (line start or after whitespace), so paths such as
    `C://x` or `~~/a//b` are not cut.
    """
    in_str = False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if in_str:
            if c == '"':
                if i + 1 < n and s[i + 1] == '"':
                    i += 2
                    continue
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "'" and i + 2 < n and s[i + 2] == "'":
            i += 3              # character literal such as ';' or '"'
            continue
        elif c == ";":
            return s[:i], s[i:], False
        elif c == "/" and s.startswith("//", i) and (i == 0 or s[i - 1] in " \t"):
            return s[:i], s[i:], False
        i += 1
    return s, "", in_str


def split_args(s: str) -> list[str]:
    """Split at whitespace that is outside strings, char literals and parentheses."""
    toks: list[str] = []
    cur: list[str] = []
    depth = 0
    in_str = False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if in_str:
            cur.append(c)
            if c == '"':
                if i + 1 < n and s[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                in_str = False
        elif c == '"':
            in_str = True
            cur.append(c)
        elif c == "'" and i + 2 < n and s[i + 2] == "'":
            cur.append(s[i:i + 3])
            i += 3
            continue
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth = max(0, depth - 1)
            cur.append(c)
        elif c in " \t" and depth == 0:
            if cur:
                toks.append("".join(cur))
                cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        toks.append("".join(cur))
    return toks


def macros_in(s: str) -> list[str]:
    """Macro names referenced in text (`&name` or `&(name)`), in order of appearance."""
    return [m.group(1) or m.group(2) for m in MACRO_RE.finditer(s)]


def is_option(tok: str) -> bool:
    """`/Verify`, `/&opt`. Not paths like `/home/x.elf`."""
    if not tok.startswith("/") or len(tok) < 2:
        return False
    body = tok[1:]
    if "/" in body or "\\" in body or "." in body:
        return False
    return body[0].isalpha() or body[0] == "&"
