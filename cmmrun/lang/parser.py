"""Lenient line-oriented PRACTICE parser.

It produces a flat list of statements plus file-level facts (labels,
subroutines, block balance). It never raises: problems become issue codes.
The interpreter builds its block structure from the same statement list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import lexer
from .vocab import Match, Vocab, load

ASSIGN_RE = re.compile(r"^(&{1,2})([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)(.*)$", re.S)
BLOCK_OPEN_RE = re.compile(r"^\((&\+|&-|&)?(\s|$)")
FUNC_RE = re.compile(r"(?<![A-Za-z0-9_.&%])([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\(")

# Canonical names of commands with special parse rules.
CALL_FILE = {"DO", "RUN", "PSTEP", "ChDir.DO", "DODECRYPT", "PEDIT", "PLIST"}
JUMPS = {"GOTO", "GOSUB", "JUMPTO"}
COND = {"IF", "WHILE"}
INLINE_AFTER_FIRST = {"IF", "WHILE", "RePeaT"}
DECLS = {"LOCAL", "PRIVATE", "GLOBAL"}
TAKERS = {"ENTRY", "PARAMETERS", "RETURNVALUES", "ENTER", "INKEY"}


@dataclass(slots=True)
class Stmt:
    kind: str                     # command | assign | block_open | block_close | label
    lineno: int
    text: str = ""                # code text (comment removed)
    cmd: str = ""                 # command as typed
    match: Match | None = None    # vocabulary match of the command
    args: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    # assign
    name: str = ""
    rhs: str = ""
    recursive: bool = False
    # derived
    macros: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    dynamic: list[str] = field(default_factory=list)   # dynamic positions, see classify_dynamic
    inline: "Stmt | None" = None   # IF c <stmt>, RePeaT n <stmt>, ...
    block_mode: str = ""           # (&+  (&-  (&
    depth: int = 0                 # block nesting depth

    @property
    def canon(self) -> str:
        return self.match.canon if self.match and self.match.status == "known" else ""


@dataclass
class ParsedFile:
    stmts: list[Stmt]
    labels: dict[str, int]               # label -> lineno (case sensitive)
    subroutines: dict[str, int]
    issues: list[str]
    n_phys: int
    n_code: int
    n_comment: int
    n_blank: int
    n_continued: int
    max_depth: int
    encoding: str = ""
    bom: bool = False


def _cmd_match(v: Vocab, cmd: str) -> Match:
    if "&" in cmd:
        # Command contains a macro: match the literal prefix only.
        lit = cmd.split("&", 1)[0].rstrip(".")
        if lit:
            m = v.command(lit)
            return Match("partial", m.canon, m.depth_known, m.ambiguous, m.abbreviated) if m.canon else m
        return Match("unknown", "", 0)
    return v.command(cmd)


def functions_in(s: str) -> list[str]:
    """Function-call names in an expression (outside strings)."""
    no_str = re.sub(r'"(?:[^"]|"")*"', '""', s)
    return FUNC_RE.findall(no_str)


MAX_INLINE = 64     # statements chained on one line (`IF a IF b ...`, `( ( (`); deeper is an issue


def parse_statement(code: str, lineno: int, v: Vocab, _depth: int = 0) -> Stmt | None:
    s = code.strip()
    if not s:
        return None
    s = lexer.DEVICE_PREFIX_RE.sub("", s, count=1).strip()
    if not s:
        return None             # a device prefix alone (`B::`) is a no-op
    if _depth > MAX_INLINE:
        return Stmt("too_deep", lineno, s)
    if m := BLOCK_OPEN_RE.match(s):
        st = Stmt("block_open", lineno, s, block_mode=m.group(1) or "")
        rest = s[m.end():].strip()
        if rest:
            st.inline = parse_statement(rest, lineno, v, _depth + 1)
        return st
    if s.startswith(")"):
        st = Stmt("block_close", lineno, s)
        rest = s[1:].strip()
        if rest:
            st.inline = parse_statement(rest, lineno, v, _depth + 1)
        return st
    if m := ASSIGN_RE.match(s):
        rhs = m.group(3).strip()
        st = Stmt("assign", lineno, s, name=m.group(2), rhs=rhs, recursive=len(m.group(1)) == 2)
        st.macros = lexer.macros_in(rhs)
        st.functions = functions_in(rhs)
        return st
    toks = lexer.split_args(s)
    if not toks:
        return None
    cmd, args = toks[0], toks[1:]
    st = Stmt("command", lineno, s, cmd=cmd, match=_cmd_match(v, cmd))
    canon = st.canon
    if canon in ("IF", "ELSE", "WHILE", "RePeaT") and args and args[-1] == "(":
        # `IF cond (`, `ELSE (`, `WHILE cond (`: the block opens on this line
        st.inline = Stmt("block_open", lineno, "(")
        args = args[:-1]
    elif canon in INLINE_AFTER_FIRST or (canon == "ELSE" and args and args[0].upper() != "IF"):
        head = args[:1] if canon != "ELSE" else []
        rest = args[len(head):]
        if rest:
            inner = parse_statement(" ".join(rest), lineno, v, _depth + 1)
            # Only accept a real command; otherwise this is part of an expression.
            if inner and inner.kind == "too_deep":
                st.inline = inner
                args = head
            elif inner and inner.kind == "command" and inner.match.status == "known":
                st.inline = inner
                args = head
    st.args = args
    st.options = [a for a in args if lexer.is_option(a)]
    st.macros = lexer.macros_in(" ".join([cmd] + args))
    st.functions = functions_in(" ".join(args))
    return st


def classify_dynamic(st: Stmt) -> list[str]:
    """Where macros make the line's meaning unknown until run time."""
    out = []
    if st.kind != "command":
        return out
    canon = st.canon
    if "&" in st.cmd:
        lit = st.cmd.split("&", 1)[0]
        out.append("subcommand" if lit.strip(".") else "command")
    if any("&" in o for o in st.options):
        out.append("option")
    pos_args = [a for a in st.args if not lexer.is_option(a)]
    if canon in CALL_FILE or canon == "ON" or canon == "GLOBALON":
        tgt = _call_target(st)
        if tgt and "&" in tgt:
            out.append("script_path")
    if canon in JUMPS and pos_args and "&" in pos_args[0]:
        out.append("jump_target")
    if not out and any("&" in a for a in pos_args):
        out.append("args_only")
    return out


def _call_target(st: Stmt) -> str:
    """The file argument of DO/RUN/... or of an ON/GLOBALON ... DO action."""
    args = [a for a in st.args if not lexer.is_option(a)]
    canon = st.canon
    if canon in ("ON", "GLOBALON"):
        up = [a.upper() for a in args]
        if "DO" in up:
            i = up.index("DO")
            return args[i + 1] if i + 1 < len(args) else ""
        return ""
    if canon == "DODECRYPT":
        return args[1] if len(args) > 1 else ""
    return args[0] if args else ""


def call_target(st: Stmt) -> str:
    return _call_target(st)


def jump_target(st: Stmt) -> str:
    """GOSUB/GOTO/JUMPTO target, or the label of an ON ... GOSUB/GOTO/JUMPTO action."""
    args = [a for a in st.args if not lexer.is_option(a)]
    if st.canon in JUMPS:
        return args[0] if args else ""
    if st.canon in ("ON", "GLOBALON"):
        up = [a.upper() for a in args]
        for j in ("GOSUB", "GOTO", "JUMPTO"):
            if j in up:
                i = up.index(j)
                return args[i + 1] if i + 1 < len(args) else ""
    return ""


def parse_text(text: str, v: Vocab | None = None) -> ParsedFile:
    v = v or load()
    stmts: list[Stmt] = []
    labels: dict[str, int] = {}
    subs: dict[str, int] = {}
    issues: list[str] = []
    n_comment = n_blank = n_code = n_cont = 0
    depth = max_depth = 0
    lines = lexer.logical_lines(text)
    n_phys = sum(ll.nphys for ll in lines)
    for ll in lines:
        raw = ll.text
        if ll.nphys > 1:
            n_cont += 1
        if not raw.strip():
            n_blank += 1
            continue
        body = raw
        if m := lexer.LABEL_RE.match(raw):
            name = m.group(1)
            if name in labels:
                issues.append("duplicate_label")
            labels[name] = ll.lineno
            stmts.append(Stmt("label", ll.lineno, name=name, depth=depth))
            body = raw[m.end():]
        code, comment, unterminated = lexer.strip_comment(body)
        if unterminated:
            issues.append("unterminated_string")
        if not code.strip():
            if comment:
                n_comment += 1
            continue
        n_code += 1
        st = parse_statement(code, ll.lineno, v)
        if st is None:
            continue
        chain = st
        while chain is not None:
            if chain.kind == "too_deep":
                issues.append("nesting_too_deep")
                chain.depth = depth
                break
            if chain.kind == "block_close":
                depth -= 1
                if depth < 0:
                    issues.append("unbalanced_close")
                    depth = 0
            chain.depth = depth
            chain.dynamic = classify_dynamic(chain)
            if chain.kind == "block_open":
                depth += 1
                max_depth = max(max_depth, depth)
            if chain.canon == "SUBROUTINE" and chain.args:
                subs[chain.args[0]] = ll.lineno
            chain = chain.inline
        stmts.append(st)
    if depth > 0:
        issues.append("unclosed_block")
    return ParsedFile(stmts, labels, subs, issues, n_phys, n_code, n_comment, n_blank, n_cont,
                      max_depth)


def parse_bytes(data: bytes, v: Vocab | None = None) -> ParsedFile:
    d = lexer.decode(data)
    pf = parse_text(d.text, v)
    pf.encoding, pf.bom = d.encoding, d.bom
    pf.issues = d.issues + pf.issues
    return pf


def walk(stmts):
    """Yield every statement including inline ones (IF c <stmt>)."""
    for st in stmts:
        while st is not None:
            yield st
            st = st.inline
