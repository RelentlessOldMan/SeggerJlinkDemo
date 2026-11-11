"""Static macro analysis.

1. Origins: for each macro used in a file, what kinds of sources its value can
   come from (literal, caller argument, target memory, file, user input, ...).
   `scan` reports these as categories only.
2. Value sets (layer 1): for macros whose every definition is a literal or a
   text substitution of other resolvable macros, the finite set of possible
   values. `check` uses this to turn DYNAMIC lines into RESOLVED ones.

The analysis is flow-insensitive and deliberately simple. PRACTICE macros are
dynamically scoped (a LOCAL is visible in callees), so a macro with no
definition in a file is looked up in the files that call it.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from .lang import lexer
from .lang.parser import ParsedFile, Stmt, functions_in, walk
from .lang.vocab import Vocab

# Function groups -> origin category.
FUNC_ORIGIN = [
    (("Data.", "Register", "Var.", "Frame.", "Break.", "STATE.", "CORE.", "TASK.", "FLASH.",
      "MMU.", "PER.", "Trace.", "Analyzer.", "Onchip.", "JTAG.", "BMC.", "SYStem.", "CPU",
      "ADDRESS.", "Y.", "sYmbol.", "Eval."), "target"),
    (("OS.FILE.", "FILE.", "OS.FIRSTFILE", "OS.NEXTFILE", "OS.DIR"), "file"),
    (("OS.", "VERSION.", "LICENSE.", "SOFTWARE.", "PRACTICE.", "INTERFACE.", "ID.", "SETUP."), "environment"),
    (("CLOCK.", "DATE.", "TIMER.", "RANDOM", "OS.TIMER"), "time"),
    (("DIALOG.", "WIN.", "INKEY", "MENU."), "user"),
    (("STRing.", "FORMAT.", "CONVert.", "MATH.", "STR.", "Var.STRing", "ENCODE."), "transform"),
    (("TRUE", "FALSE"), "literal"),
]
ORIGINS = ("literal", "caller", "callee_return", "target", "file", "environment", "time", "user",
           "loop", "global", "inherited", "transform_of_unknown", "other_function")
STATIC_OK = {"literal"}
PPD_CALLS = {"OS.PPD()", "OS.PresentPracticeDirectory()", "OS.PRESENTPRACTICEDIRECTORY()", "os.ppd()"}


def func_origin(canon_or_typed: str) -> str:
    for prefixes, cat in FUNC_ORIGIN:
        for p in prefixes:
            if canon_or_typed == p.rstrip(".") or canon_or_typed.startswith(p):
                return cat
    return "other_function"


@dataclass
class MacroDefs:
    """All definitions of one macro name in one file."""
    kinds: set[str] = field(default_factory=set)         # direct origins
    refs: set[str] = field(default_factory=set)          # macros the value is built from
    literals: list[str] = field(default_factory=list)    # literal / substitutable RHS texts
    self_ref: bool = False
    shapes: list[Stmt] = field(default_factory=list)


@dataclass
class FileFlow:
    defs: dict[str, MacroDefs]
    globals_declared: set[str]
    origins: dict[str, set[str]] = field(default_factory=dict)


LIT_NUM = re.compile(r"^[A-Za-z]{0,4}:?(0x[0-9A-Fa-f_]+|0y[01_]+|[0-9][0-9_]*\.?[0-9]*(ms|us|ns|s|ks)?)$")


def expr_kinds(rhs: str, v: Vocab) -> tuple[set[str], set[str], bool]:
    """(direct origin kinds, macro refs, substitutable) for an assignment RHS.

    "substitutable" means the value is pure text: literals and macro references
    with no function calls, so a finite set of inputs gives a finite set of values.
    Arithmetic is still substitutable (we keep the text; the interpreter evaluates).
    """
    kinds: set[str] = set()
    refs = set(lexer.macros_in(rhs))
    funcs = functions_in(rhs)
    for fn in funcs:
        m = v.function(fn)
        kinds.add(func_origin(m.canon if m.status == "known" else fn))
    if not funcs and not refs:
        kinds.add("literal")
    substitutable = not funcs or all(k == "literal" for k in kinds)
    return kinds, refs, substitutable


def file_flow(pf: ParsedFile, v: Vocab) -> FileFlow:
    defs: dict[str, MacroDefs] = {}
    globals_declared: set[str] = set()
    prev: Stmt | None = None

    def d(name) -> MacroDefs:
        return defs.setdefault(name, MacroDefs())

    for st in walk(pf.stmts):
        if st.kind == "assign":
            md = d(st.name)
            kinds, refs, subst = expr_kinds(st.rhs, v)
            md.kinds |= kinds
            md.refs |= refs
            if st.name in refs:
                md.self_ref = True
            if st.rhs.strip().replace(" ", "") in PPD_CALLS:
                md.literals.append("~~~~")  # the script's own directory, same as the ~~~~ prefix
            elif subst:
                md.literals.append(st.rhs)
            else:
                md.literals.append(None)  # marks "not finite"
            md.shapes.append(st)
        elif st.kind == "command":
            c = st.canon
            names = [a[1:] for a in st.args if a.startswith("&") and lexer.MACRO_RE.fullmatch(a)]
            names = [n.strip("()") for n in names]
            if c == "GLOBAL":
                globals_declared |= set(names)
            elif c in ("ENTRY", "PARAMETERS"):
                after_call = prev is not None and prev.canon in ("GOSUB", "DO", "RUN")
                for n in names:
                    md = d(n)
                    md.kinds.add("callee_return" if after_call else "caller")
                    md.literals.append(None)
                    md.shapes.append(st)
            elif c == "RETURNVALUES":
                for n in names:
                    d(n).kinds.add("callee_return")
                    d(n).literals.append(None)
            elif c in ("READ",):
                for n in names:
                    d(n).kinds.add("file")
                    d(n).literals.append(None)
            elif c in ("ENTER", "INKEY") or c.startswith("DIALOG"):
                for n in names:
                    d(n).kinds.add("user")
                    d(n).literals.append(None)
            elif c == "SPRINTF" or c == "Var.ASSIGN":
                for n in names[:1]:
                    d(n).kinds.add("transform_of_unknown")
                    d(n).literals.append(None)
        prev = st
    return FileFlow(defs, globals_declared)


def resolve_origins(ff: FileFlow, global_kinds: dict[str, set[str]]) -> None:
    """Fixpoint: expand macro references into origin kinds. Macros that reference each other
    (`&j=&i+1` / `&i=&j`) end up with the union of their kinds, whatever the definition order."""
    out: dict[str, set[str]] = {}
    for name, md in ff.defs.items():
        ks = set(md.kinds)
        if md.self_ref:
            ks.add("loop")
        for r in md.refs:
            if r not in ff.defs:
                ks.add("global" if r in global_kinds else "inherited")
        out[name] = ks
    changed = True
    while changed:
        changed = False
        for name, md in ff.defs.items():
            ks = out[name]
            before = len(ks)
            for r in md.refs:
                if r != name and r in out:
                    ks |= out[r]
            if len(ks) != before:
                changed = True
    ff.origins = out


def origins_of(ff: FileFlow, names, global_kinds) -> set[str]:
    ks: set[str] = set()
    for n in names:
        if n in ff.origins:
            ks |= ff.origins[n]
        elif n in global_kinds:
            ks.add("global")
        else:
            ks.add("inherited")
    return ks or {"literal"}


# ---------------------------------------------------------------- value sets

MAX_VALUE = 4096     # longer substituted texts are treated as unknown


def substitute(text: str, env: dict[str, set[str]], cap: int) -> set[str] | None:
    """All texts obtained by replacing macros with their possible values."""
    names = []
    for n in lexer.macros_in(text):
        if n not in names:
            names.append(n)
    if not names:
        return {text}
    choices = []
    for n in names:
        vals = env.get(n)
        if vals is None:
            return None
        choices.append(sorted(vals))
    total = 1
    for ch in choices:
        total *= max(1, len(ch))
        if total > cap:
            return None
    out = set()
    pat = re.compile("|".join(rf"&\({re.escape(n)}\)|&{re.escape(n)}(?![A-Za-z0-9_])"
                              for n in sorted(names, key=len, reverse=True)))
    for combo in itertools.product(*choices):
        vals = dict(zip(names, combo))
        # one pass over the original text: a value that contains macro text is not expanded again
        t = pat.sub(lambda m: vals[m.group(0)[1:].strip("()")], text)
        if len(t) > MAX_VALUE:
            return None
        out.add(t)
    return out


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('""', '"')
    return s


def value_sets(ff: FileFlow, inherited: dict[str, set[str]], cap: int = 64) -> dict[str, set[str]]:
    """Finite value sets for macros whose definitions are all substitutable.

    `inherited` holds value sets provided from outside (caller arguments for
    ENTRY/PARAMETERS, GLOBALs, LOCALs of callers). A macro with an ENTRY
    definition takes its values from `inherited` if present.
    """
    env: dict[str, set[str]] = {}
    for n, vals in inherited.items():
        if n not in ff.defs:
            env[n] = set(vals)
    for _ in range(4):  # a few rounds are enough for chains of assignments
        changed = False
        for name, md in ff.defs.items():
            if md.self_ref:
                continue
            vals: set[str] = set()
            ok = True
            for lit in md.literals:
                if lit is None:
                    if name in inherited:
                        vals |= inherited[name]
                        continue
                    ok = False
                    break
                sub = substitute(lit, env, cap)
                if sub is None:
                    ok = False
                    break
                vals |= {_strip_quotes(s) for s in sub}
            if ok and vals and len(vals) <= cap and env.get(name) != vals:
                env[name] = vals
                changed = True
        if not changed:
            break
    return env
