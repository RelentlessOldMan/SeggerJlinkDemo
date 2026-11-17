"""The PRACTICE interpreter.

A script is parsed with the same lenient parser `scan` and `check` use, then
flattened into a list of statements. Blocks `( ... )` are matched once. The
interpreter walks the list; IF / WHILE / RePeaT / SUBROUTINE take the next
"unit" (a statement or a block) as their body.

Macros are text. Before a statement runs, every visible `&name` is replaced
by its text (also inside strings), then the line is parsed again. Scoping
follows TRACE32 (checked in the simulator, see docs/practice-semantics.md):

  - each block is a scope for LOCAL and PRIVATE;
  - LOCAL is visible in nested blocks, GOSUB subroutines and DO'd scripts;
  - PRIVATE is visible in nested blocks only, not across a call;
  - assigning to an unknown macro creates it at the current call level
    (script or GOSUB), not in the current block.
"""
from __future__ import annotations

import pathlib
import re
import sys
import time
from array import array
from collections import OrderedDict
from dataclasses import dataclass, field

from ..backends.base import Backend
from ..backends.images import load_image
from ..lang import lexer
from ..lang.parser import Stmt, parse_bytes, parse_line, parse_statement
from ..lang.vocab import Vocab, abbrev_ok, load
from . import functions
from .expr import Evaluator, tokenize
from .values import (Addr, Bool, Flt, Num, PracticeError, Rng, Str, TargetError, Time, UnsupportedError, Value, echo,
                     macro_text, plain, to_int, truth)


# ---------------------------------------------------------------- control-flow signals
class Goto(Exception):
    def __init__(self, index: int, level: "Frame"):
        self.index, self.level = index, level


class Return(Exception):
    def __init__(self, values: list[str]):
        self.values = values


class EndDo(Exception):
    def __init__(self, values: list[str]):
        self.values = values


class EndAll(Exception):
    """END, STOP, or the end of RUN."""
    def __init__(self, reason: str = "END"):
        self.reason = reason


class Timeout(Exception):
    pass


class ScriptFailure(Exception):
    """An error with its location, raised out of Interpreter.run."""
    def __init__(self, exc: Exception, where: str):
        super().__init__(f"{where}: {exc}")
        self.exc, self.where = exc, where


# ---------------------------------------------------------------- program
# Scripts larger than this are kept as text plus a compact index and parsed again line by
# line when run (see LazyIns); smaller ones are parsed once into Stmt objects.
LAZY_BYTES = 2 * 2**20


class Program:
    def __init__(self, path: pathlib.Path, vocab: Vocab):
        self.path = path
        self.vocab = vocab
        data = path.read_bytes()
        if len(data) > LAZY_BYTES:
            self._init_lazy(data)
            return
        pf = parse_bytes(data, vocab)
        if "unbalanced_close" in pf.issues or "unclosed_block" in pf.issues:
            raise PracticeError(f"{path.name}: unbalanced ( ) blocks")
        if "nesting_too_deep" in pf.issues:
            raise PracticeError(f"{path.name}: too many statements chained on one line")
        self.ins: list[Stmt] = []
        for st in pf.stmts:
            _flatten(st, self.ins)
        self.match: dict[int, int] = {}
        stack = []
        for i, st in enumerate(self.ins):
            if st.kind == "block_open":
                stack.append(i)
            elif st.kind == "block_close":
                self.match[stack.pop()] = i
        self.labels = {st.name: i for i, st in enumerate(self.ins) if st.kind == "label"}
        self.subs = {st.args[0]: i for i, st in enumerate(self.ins)
                     if st.kind == "command" and st.canon == "SUBROUTINE" and st.args}

    def _init_lazy(self, data: bytes):
        """One streaming pass: build the index (where each instruction's line starts, block
        matches, labels, subroutines) without keeping any statement."""
        d = lexer.decode(data)
        text = lexer.normalize_eols(d.text)
        del data, d
        line_start, line_no = array("q"), array("q")     # per logical line that has instructions
        ins_line, ins_k = array("l"), array("H")         # per instruction: its line, index in that line
        match = array("l")                               # per instruction: matching close, or -1
        stack: list[int] = []
        self.labels, self.subs = {}, {}
        for start, raw, lineno, _ in lexer.iter_logical(text):
            if not raw.strip():
                continue
            flat = _line_ins(raw, lineno, self.vocab)
            if not flat:
                continue
            li = len(line_start)
            line_start.append(start)
            line_no.append(lineno)
            for k, st in enumerate(flat):
                i = len(ins_line)
                ins_line.append(li)
                ins_k.append(k)
                match.append(-1)
                if st.kind == "too_deep":
                    raise PracticeError(f"{self.path.name}: too many statements chained on one line")
                if st.kind == "block_open":
                    stack.append(i)
                elif st.kind == "block_close":
                    if not stack:
                        raise PracticeError(f"{self.path.name}: unbalanced ( ) blocks")
                    match[stack.pop()] = i
                elif st.kind == "label":
                    self.labels[st.name] = i
                elif st.kind == "command" and st.canon == "SUBROUTINE" and st.args:
                    self.subs[st.args[0]] = i
        if stack:
            raise PracticeError(f"{self.path.name}: unbalanced ( ) blocks")
        self.match = match
        self.ins = LazyIns(text, line_start, line_no, ins_line, ins_k, self.vocab)

    def unit_end(self, i: int) -> int:
        """Index just after the unit (statement with its body) starting at i."""
        n = len(self.ins)
        if i >= n:
            return n
        st = self.ins[i]
        if st.kind == "block_open":
            return self.match[i] + 1
        if st.kind != "command":
            return i + 1
        c = st.canon
        if c == "IF":
            j = i + 1 if st.inline else self.unit_end(i + 1)
            while j < n and self.ins[j].kind == "command" and self.ins[j].canon == "ELSE":
                e = self.ins[j]
                if e.inline:
                    return j + 1
                j = self.unit_end(j + 1)
                if not (e.args and e.args[0].upper() == "IF"):
                    return j
            return j
        if c == "WHILE":
            return i + 1 if st.inline else self.unit_end(i + 1)
        if c == "RePeaT":
            if st.inline:
                return i + 1
            j = self.unit_end(i + 1)
            if not st.args and j < n and self.ins[j].kind == "command" and self.ins[j].canon == "WHILE" \
                    and not self.ins[j].inline:
                return j + 1
            return j
        if c == "SUBROUTINE":
            return self.unit_end(i + 1)
        return i + 1


def _flatten(st: Stmt, out: list):
    """Append `st` and its inline chain to `out` as separate instructions where blocks need it."""
    if st.kind in ("block_open", "block_close"):
        inner, st.inline = st.inline, None
        out.append(st)
        if inner:
            _flatten(inner, out)
        return
    if st.inline is not None:
        # IF c <stmt>: the inline statement is the body; flatten its own blocks too.
        inner = st.inline
        if inner.kind in ("block_open", "block_close"):
            st.inline = None
            out.append(st)
            _flatten(inner, out)
            return
    out.append(st)


def _line_ins(raw: str, lineno: int, vocab: Vocab) -> list[Stmt]:
    """The instructions of one logical line, as the eager parse + flatten would give them."""
    label, st, _, _, _ = parse_line(raw, lineno, vocab)
    out: list[Stmt] = []
    if label is not None:
        out.append(Stmt("label", lineno, name=label))
    if st is not None:
        _flatten(st, out)
    return out


class LazyIns:
    """The instruction list of a big script, parsed on demand from its text.

    Memory is the text plus about 30 bytes per instruction, instead of several hundred bytes
    per parsed statement. Recently used lines stay parsed, so loops do not parse again."""
    CACHE_LINES = 8192

    def __init__(self, text, line_start, line_no, ins_line, ins_k, vocab):
        self.text, self.line_start, self.line_no = text, line_start, line_no
        self.ins_line, self.ins_k, self.vocab = ins_line, ins_k, vocab
        self.cache: OrderedDict[int, list[Stmt]] = OrderedDict()

    def __len__(self):
        return len(self.ins_line)

    def __getitem__(self, i: int) -> Stmt:
        if i < 0:
            i += len(self.ins_line)
        li = self.ins_line[i]
        flat = self.cache.get(li)
        if flat is None:
            _, raw, lineno, _ = next(lexer.iter_logical(self.text, self.line_start[li], self.line_no[li]))
            flat = _line_ins(raw, lineno, self.vocab)
            self.cache[li] = flat
            if len(self.cache) > self.CACHE_LINES:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(li)
        return flat[self.ins_k[i]]

    def __iter__(self):
        for i in range(len(self.ins_line)):
            yield self[i]


# ---------------------------------------------------------------- frames
@dataclass
class Frame:
    kind: str                         # script | gosub | block
    parent: "Frame | None"
    prog: Program
    locals: dict[str, str] = field(default_factory=dict)
    privates: dict[str, str] = field(default_factory=dict)
    entry_args: list[str] = field(default_factory=list)
    on_error: tuple[str, str] | None = None     # (action, target)

    def call_level(self) -> "Frame":
        f = self
        while f.kind == "block":
            f = f.parent
        return f


# ---------------------------------------------------------------- host files
class Files:
    def __init__(self):
        self.h: dict[str, dict] = {}

    def open(self, ch: str, path: pathlib.Path, mode: str):
        if mode == "r":
            lines = path.read_bytes().decode("cp1252", errors="replace").replace("\r\n", "\n").split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            self.h[ch] = {"mode": "r", "lines": lines, "pos": 0, "eof": False}
        else:
            self.h[ch] = {"mode": mode, "f": open(path, "a" if mode == "a" else "w", encoding="cp1252",
                                                  errors="replace", newline="\n")}

    def _get(self, ch):
        if ch not in self.h:
            raise PracticeError(f"file #{ch} is not open")
        return self.h[ch]

    def readline(self, ch: str) -> str:
        f = self._get(ch)
        if f["pos"] >= len(f["lines"]):
            f["eof"] = True
            return ""
        f["pos"] += 1
        return f["lines"][f["pos"] - 1]

    def write(self, ch: str, text: str):
        f = self._get(ch)
        if f["mode"] == "r":
            raise PracticeError(f"file #{ch} is open for reading")
        f["f"].write(text + "\n")

    def eof(self, ch: str) -> bool:
        return self.h.get(ch, {}).get("eof", True)

    def close(self, ch: str):
        f = self.h.pop(ch, None)
        if f and "f" in f:
            f["f"].close()

    def close_all(self):
        for ch in list(self.h):
            self.close(ch)


# ---------------------------------------------------------------- commands
COMMANDS: dict[str, str] = {}       # canonical name -> method name
TARGET_COMMANDS: set[str] = set()   # talk to the target (the rest is pure language)
NEEDS_CONNECTION: set[str] = set()  # fail with a target error unless SYStem.Up was done

IGNORED = set("""
WinCLEAR WinPOS WinPAGE WinRESIZE WinTOP WinEXT WinOverlay WinFreeze WinPrint
AREA AREA.view AREA.Create AREA.Select AREA.CLEAR AREA.Delete AREA.CLOSE AREA.OPEN AREA.RESet
SCREEN SCREEN.ON SCREEN.OFF SCREEN.display SCREEN.WAIT SCREEN.ALways
Mode.Hll Mode.Asm Mode.Mix TOOLBAR STATUSBAR MENU.ReProgram MENU.RESet BEEP
List List.auto Register.view Data.List Data.dump Data.View PER.view SETUP.Var SETUP.LIST
TITLE FramePOS SYStem.Option.ResBreak SYStem.Option.EnReset SYStem.Option.IMASKASM
SYStem.Option.IMASKHLL SYStem.MemAccess SYStem.CpuAccess
PLIST
""".split())
# Where the debugger GUI looks for source files: no effect on the target or on the script.
IGNORED |= {"sYmbol.SourcePATH" + s for s in """
 .DOWN .Delete .List .RESet .Set .SetBaseDir .SetCache .SetCachedDir .SetCachedDirCache
 .SetCachedDirIgnoreCache .SetCachedDirIgnoreCase .SetDir .SetDynamicDir .SetMasterDir .SetRecurseDir
 .SetRecurseDirCache .SetRecurseDirIgnoreCase .Translate .TranslateSUBpath .UP .Verbose""".split() + [""]}


def command(*names: str, target: bool = False, connected: bool = False):
    def deco(f):
        for n in names:
            COMMANDS[n] = f.__name__
            if target or connected:
                TARGET_COMMANDS.add(n)
            if connected:
                NEEDS_CONNECTION.add(n)
        return f
    return deco


HEX_RE = re.compile(r"0x[0-9A-Fa-f][0-9A-Fa-f_]*")
# PRINTF conversions: %[flags][width|*][.precision][size]conversion
PRINTF_RE = re.compile(r"%([-+ #0]*)(\*|\d+)?(?:\.(\d+))?(hh|h|ll|l)?([diuxXocsfeEgG%])")
# Bits a size modifier keeps (simulator: no modifier and ll are 64-bit, l is 32-bit and shown
# unsigned even by %d, h 16-bit, hh 8-bit).
PRINTF_BITS = {None: 64, "ll": 64, "l": 32, "h": 16, "hh": 8}


def printf_text(fmt: str, vals: list) -> str:
    """C-style formatting as PRINTF does it in TRACE32 (checked against the simulator)."""
    vals = list(vals)

    def take():
        if not vals:
            raise PracticeError("PRINTF: expression expected (more conversions than arguments)")
        return vals.pop(0)

    def conv(m: re.Match) -> str:
        flags, width, prec, size, c = m.groups()
        if c == "%":
            return "%"
        if width == "*":
            width = str(to_int(take(), "width"))
        v = take()
        spec = "%" + flags + (width or "") + ("." + prec if prec is not None else "")
        if c == "s":
            if not isinstance(v, Str):
                raise PracticeError("PRINTF: the expression has a wrong result type (%s needs a string)")
            return (spec + "s") % v.v
        if isinstance(v, Str):
            raise PracticeError(f"PRINTF: the expression has a wrong result type (%{c} needs a number)")
        if c in "feEgG":
            return (spec + c) % float(v.v if isinstance(v, Flt) else to_int(v, "value"))
        n = int(v.v) if isinstance(v, (Bool, Flt)) else to_int(v, "value")
        bits = PRINTF_BITS[size]
        n &= (1 << bits) - 1
        if c in "di" and bits == 64 and n >> 63:
            n -= 1 << 64                     # only the 64-bit forms are signed
        if c == "c":
            return (spec + "c") % chr(n & 0xFF)
        return (spec + ("d" if c in "iu" else c)) % n

    out = PRINTF_RE.sub(conv, fmt)
    if vals:
        raise PracticeError("PRINTF: no more arguments expected")
    return out


SIZES = {"Byte": 1, "Word": 2, "Long": 4, "Quad": 8, "Short": 2, "TByte": 3, "PByte": 5, "HByte": 6,
         "SByte": 7}
PRINT_FMT = {"Decimal": "dec", "Hex": "hex", "BINary": "bin", "Ascii": "ascii"}
FORMATS = list(SIZES) + list(PRINT_FMT) + ["LE", "BE", "CONTinue", "ERROR", "WARNING", "COLOR", "LINE"]


MAX_CALL_DEPTH = 100


def _os_message(e: OSError) -> str:
    name = pathlib.Path(e.filename).name if e.filename else ""
    return f"{name}: {e.strerror or e}" if name else str(e.strerror or e)


def _fmt_name(tok: str) -> str:
    """`%L` -> Long, `%Decimal` -> Decimal, `%CONT` -> CONTinue."""
    typed = tok[1:]
    for full in FORMATS:
        if full.upper() == typed.upper():
            return full
    for full in FORMATS:
        if abbrev_ok(typed, full):
            return full
    return typed


class Interpreter:
    def __init__(self, backend: Backend, *, out=None, cwd: str | None = None, timeout: float = 600.0,
                 t32_sys: str | None = None, vocab: Vocab | None = None):
        self.backend = backend
        self.vocab = vocab or load()
        self.out_lines: list[str] = []
        self._out_cb = out
        self.cwd = pathlib.Path(cwd or ".").resolve()
        self.timeout = timeout
        self.t32_sys = t32_sys
        self.globals: dict[str, str] = {}
        self.frame: Frame | None = None
        self._depth = 0
        self.programs: dict[pathlib.Path, Program] = {}
        self.files = Files()
        self.found = False
        self.big_endian = False
        self.warnings: list[str] = []
        self._t0 = 0.0
        self._v0 = 0.0
        self._cur: tuple[Program, Stmt] | None = None
        self._in_handler = False
        # Symbols made by sYmbol.CREATE.Label: they exist once sYmbol.CREATE.Done runs (as in TRACE32).
        self.symbols: dict[str, Addr] = {}
        self.new_symbols: dict[str, Addr] = {}
        self.stdin = sys.stdin              # where ENTER reads from
        self.quit_code = None               # set by QUIT: TRACE32 exits with it (simulator-checked)
        self.elf_loaded = False             # its debug symbols are not read (see sYmbol.EXIST)
        self.ev = Evaluator(self._call_function, self.symbols.get)

    # ---- output
    def emit(self, text: str, cont: bool = False):
        """One PRINT/PRINTF line. %CONTinue appends to the previous line (simulator: PRINT "k"
        then PRINT %CONTinue "l=" shows "kl=")."""
        if cont and self.out_lines:
            self.out_lines[-1] += text
            if self._out_cb:
                self._out_cb(text, False)
        else:
            self.out_lines.append(text)
            if self._out_cb:
                self._out_cb(text, True)

    # ---- entry point
    def run(self, script: str | pathlib.Path, args: list[str] | None = None) -> list[str]:
        """Run a script. Returns the values it passed to ENDDO."""
        self._t0, self._v0 = time.monotonic(), self.backend.now()
        path = self.resolve_script(str(script), caller=None)
        try:
            while True:
                try:
                    return self._do(path, args or [], parent=None)
                except _RunRequest as r:
                    path, args = r.path, r.args
                    self.frame = None
        except EndAll as e:
            if e.reason != "END":
                self.warnings.append(e.reason)
            return []
        except (PracticeError, TargetError, Timeout) as e:
            raise ScriptFailure(e, self._where()) from e
        finally:
            self.files.close_all()

    def _where(self) -> str:
        if not self._cur:
            return "?"
        prog, st = self._cur
        return f"{prog.path.name}:{st.lineno}"

    # ---- paths
    def script_dir(self) -> pathlib.Path:
        f = self.frame
        return f.prog.path.parent if f else self.cwd

    def _expand(self, text: str) -> str:
        t = text.strip()
        if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
            t = t[1:-1].replace('""', '"')
        t = t.replace("\\", "/")
        if t.startswith("~~~~"):
            return str(self.script_dir()) + "/" + t[4:].lstrip("/")
        if t.startswith("~~~"):
            import tempfile
            return tempfile.gettempdir() + "/" + t[3:].lstrip("/")
        if t.startswith("~~"):
            if not self.t32_sys:
                raise UnsupportedError(f"{text}: '~~' is the TRACE32 system directory, which cmmrun does not have")
            return self.t32_sys + "/" + t[2:].lstrip("/")
        return t

    def resolve_host_path(self, text: str) -> pathlib.Path:
        p = pathlib.Path(self._expand(text))
        if p.is_absolute():
            return p
        for base in (self.cwd, self.script_dir()):
            if (base / p).exists():
                return (base / p).resolve()
        return (self.cwd / p).resolve()

    def resolve_script(self, text: str, caller: Frame | None) -> pathlib.Path:
        t = self._expand(text)
        if not pathlib.PurePosixPath(t).suffix:
            t += ".cmm"
        p = pathlib.Path(t)
        cands = [p] if p.is_absolute() else [self.cwd / p] + ([caller.prog.path.parent / p] if caller else [])
        for c in cands:
            if c.is_file():
                return c.resolve()
        raise PracticeError(f"script not found: {text}")

    def program(self, path: pathlib.Path) -> Program:
        if path not in self.programs:
            self.programs[path] = Program(path, self.vocab)
        return self.programs[path]

    # ---- macros
    def _lookup(self, name: str) -> tuple[dict, str] | None:
        f, crossed = self.frame, False
        while f is not None:
            if name in f.locals:
                return f.locals, f.locals[name]
            if not crossed and name in f.privates:
                return f.privates, f.privates[name]
            if f.kind != "block":
                crossed = True
            f = f.parent
        if name in self.globals:
            return self.globals, self.globals[name]
        return None

    def get_macro(self, name: str) -> str | None:
        r = self._lookup(name)
        return r[1] if r else None

    def set_macro(self, name: str, text: str):
        r = self._lookup(name)
        if r:
            r[0][name] = text
        else:
            self.frame.call_level().locals[name] = text

    def substitute(self, text: str) -> str:
        """Replace visible macros by their text. Inside a string literal the macro text is
        inserted as characters: its quotes do not end the string (TRACE32 behaves so)."""
        if "&" not in text:
            return text
        out, i, n, in_str = [], 0, len(text), False
        while i < n:
            c = text[i]
            if c == '"':
                if in_str and i + 1 < n and text[i + 1] == '"':
                    out.append('""')
                    i += 2
                    continue
                in_str = not in_str
            elif c == "&":
                m = lexer.MACRO_RE.match(text, i)
                if m:
                    v = self.get_macro(m.group(1) or m.group(2))
                    if v is not None:
                        out.append(v.replace('"', '""') if in_str else v)
                        i = m.end()
                        continue
            out.append(c)
            i += 1
        return "".join(out)

    # ---- expressions
    def _call_function(self, name: str, args: list[str], ev: Evaluator) -> Value:
        m = self.vocab.function(name)
        canon = m.canon if m.status in ("known", "partial") else name
        if canon not in functions.REGISTRY and m.status != "known":
            canon = name
        if canon in functions.TARGET_FUNCTIONS:
            self._need_target(canon + "()")
        return functions.call(self, canon, args, ev)

    def eval(self, text: str) -> Value:
        return self.ev.eval(text)

    def _need_target(self, what: str):
        if not self.backend.connected:
            raise TargetError(f"{what}: no target connection (SYStem.Up first)")

    # ---- running
    def _check_time(self):
        if time.monotonic() - self._t0 > self.timeout or self.backend.now() - self._v0 > self.timeout:
            raise Timeout(f"timeout after {self.timeout:g} s")

    def _enter(self):
        self._depth += 1
        if self._depth > MAX_CALL_DEPTH:
            self._depth -= 1
            raise PracticeError(f"PRACTICE stack overflow: more than {MAX_CALL_DEPTH} nested DO/GOSUB calls")

    def _do(self, path: pathlib.Path, args: list[str], parent: Frame | None) -> list[str]:
        prog = self.program(path)
        self._enter()
        saved = self.frame
        self.frame = Frame("script", parent, prog, entry_args=list(args))
        try:
            self._run_level(prog, 0, len(prog.ins))
            return []
        except EndDo as e:
            return e.values
        except Return:
            raise PracticeError("#pra_stackun: PRACTICE stack underflow (RETURN outside a subroutine)") from None
        finally:
            self.frame = saved
            self._depth -= 1

    def _gosub(self, prog: Program, name: str, args: list[str]) -> list[str]:
        if name in prog.labels:
            start, end = prog.labels[name] + 1, len(prog.ins)
        elif name in prog.subs:
            i = prog.subs[name]
            start, end = i + 1, prog.unit_end(i)
        else:
            raise PracticeError(f"label not found: {name}")
        self._enter()
        saved = self.frame
        self.frame = Frame("gosub", saved, prog, entry_args=list(args))
        try:
            self._run_level(prog, start, end)
            return []
        except Return as r:
            return r.values
        finally:
            self.frame = saved
            self._depth -= 1

    def _run_level(self, prog: Program, start: int, end: int):
        """Run [start, end) at the current call level; GOTOs to this level jump here."""
        level = self.frame.call_level()
        pc = start
        while True:
            try:
                self._run_range(prog, pc, end)
                return
            except Goto as g:
                if g.level is not level:
                    raise
                pc = g.index

    def _run_range(self, prog: Program, start: int, end: int):
        pc = start
        while pc < end:
            pc = self._exec_unit(prog, pc)

    def _block(self, prog: Program, i: int):
        """Run the block starting at index i in a new scope."""
        saved = self.frame
        self.frame = Frame("block", saved, prog)
        try:
            self._run_range(prog, i + 1, prog.match[i])
        finally:
            self.frame = saved

    def _body(self, prog: Program, i: int, inline: Stmt | None):
        """Run a body: an inline statement, a block, or the single statement at i."""
        if inline is not None:
            self._exec_stmt(prog, inline, i)
        elif i < len(prog.ins) and prog.ins[i].kind == "block_open":
            self._block(prog, i)
        else:
            self._exec_unit(prog, i)

    def _exec_unit(self, prog: Program, i: int) -> int:
        st = prog.ins[i]
        self._check_time()
        if st.kind == "label":
            return i + 1
        if st.kind == "block_open":
            self._block(prog, i)
            return prog.match[i] + 1
        if st.kind == "block_close":
            return i + 1
        if st.kind == "command":
            c = st.canon
            if c == "IF":
                return self._if(prog, i)
            if c == "WHILE":
                return self._while(prog, i)
            if c == "RePeaT":
                return self._repeat(prog, i)
            if c == "SUBROUTINE":
                return prog.unit_end(i)      # only runs when called
            if c == "ELSE":
                raise PracticeError("ELSE without IF")
        self._exec_stmt(prog, st, i)
        return i + 1

    def _cond(self, prog, st, text) -> bool:
        self._cur = (prog, st)
        return self._guard(lambda: truth(self.eval(self.substitute(text))), prog, st, default=False)

    def _if(self, prog: Program, i: int) -> int:
        st = prog.ins[i]
        n = len(prog.ins)
        taken = self._cond(prog, st, " ".join(st.args))
        body_end = i + 1 if st.inline else prog.unit_end(i + 1)
        if taken:
            self._body(prog, i + 1, st.inline)
        j = body_end
        while j < n and prog.ins[j].kind == "command" and prog.ins[j].canon == "ELSE":
            e = prog.ins[j]
            is_elif = bool(e.args) and e.args[0].upper() == "IF"
            e_end = j + 1 if e.inline else prog.unit_end(j + 1)
            if not taken:
                if is_elif:
                    if self._cond(prog, e, " ".join(e.args[1:])):
                        taken = True
                        self._body(prog, j + 1, e.inline)
                else:
                    taken = True
                    self._body(prog, j + 1, e.inline)
            j = e_end
            if not is_elif:
                break
        return j

    def _while(self, prog: Program, i: int) -> int:
        st = prog.ins[i]
        while self._cond(prog, st, " ".join(st.args)):
            self._check_time()
            self._body(prog, i + 1, st.inline)
        return prog.unit_end(i)

    def _repeat(self, prog: Program, i: int) -> int:
        st = prog.ins[i]
        end = prog.unit_end(i)
        if st.args:
            self._cur = (prog, st)
            count = self._guard(lambda: to_int(self.eval(self.substitute(" ".join(st.args))), "RePeaT count"),
                                prog, st, default=0)
            for _ in range(max(0, count)):
                self._check_time()
                self._body(prog, i + 1, st.inline)
            return end
        body_end = i + 1 if st.inline else prog.unit_end(i + 1)
        tail = prog.ins[body_end] if body_end < end else None     # RePeaT ( ... ) WHILE <cond>
        while True:
            self._check_time()
            self._body(prog, i + 1, st.inline)
            if tail is not None and not self._cond(prog, tail, " ".join(tail.args)):
                return end

    # ---- one statement, with ON ERROR handling
    def _guard(self, fn, prog, st, default=None):
        try:
            return fn()
        except OSError as oe:
            e = PracticeError(_os_message(oe))
        except (PracticeError, TargetError) as pe:
            if isinstance(pe, UnsupportedError):
                raise
            e = pe
        h = self._handler()
        if h is None or self._in_handler:
            raise e
        frame, (action, target) = h
        self.last_error = str(e)
        if action == "CONTINUE":
            return default
        if action == "GOTO":
            idx = frame.prog.labels.get(target)
            if idx is None:
                raise PracticeError(f"ON ERROR GOTO: label not found: {target}") from e
            raise Goto(idx, frame) from None
        self._in_handler = True
        try:
            if action == "GOSUB":
                self._gosub(frame.prog, target, [])
            elif action == "DO":
                self._do(self.resolve_script(target, frame), [], self.frame)
        finally:
            self._in_handler = False
        return default

    def _handler(self):
        f = self.frame
        while f is not None:
            if f.kind != "block" and f.on_error is not None:
                if f.on_error[0] == "DEFAULT":
                    return None
                return f, f.on_error
            f = f.parent
        return None

    def _exec_stmt(self, prog: Program, st: Stmt, i: int):
        self._cur = (prog, st)
        self._guard(lambda: self._exec(prog, st, i), prog, st)

    def _exec(self, prog: Program, st: Stmt, i: int):
        if st.kind == "assign":
            rhs = self.substitute(st.rhs)
            if st.recursive:
                for _ in range(20):
                    nxt = self.substitute(rhs)
                    if nxt == rhs:
                        break
                    rhs = nxt
            self.set_macro(st.name, macro_text(self.eval(rhs)) if rhs.strip() else "")
            return
        if st.kind != "command":
            return
        c = st.canon
        if c in ("LOCAL", "PRIVATE", "GLOBAL"):
            return self.c_declare(st)
        if c in ("ENTRY", "PARAMETERS", "RETURNVALUES", "ENTER"):
            return getattr(self, "c_" + c.lower())(st)
        if "&" in st.text:
            st2 = parse_statement(self.substitute(st.text), st.lineno, self.vocab)
            if st2 is None:
                return
            if st2.kind != "command" or st2.inline is not None:
                if st2.kind == "assign" or st2.inline is not None:
                    raise UnsupportedError(f"macro expands to a statement cmmrun cannot run here: {st2.text!r}")
                return
            st = st2
            c = st.canon
        if c in IGNORED:
            return
        meth = COMMANDS.get(c)
        if meth is None:
            if not c:
                # Not a TRACE32 command at all: an ordinary script error (ON ERROR can catch it).
                raise PracticeError(f"unknown command {st.cmd}")
            raise UnsupportedError(f"command {c} is not supported by cmmrun")
        if c in NEEDS_CONNECTION:
            self._need_target(c)
        getattr(self, meth)(st)

    # ---- helpers for commands
    def _argtext(self, st: Stmt) -> str:
        return " ".join(st.args)

    def _items(self, st: Stmt, keep_words=False):
        return self.ev.items(self._argtext(st), keep_words=keep_words)

    @staticmethod
    def _raw_args(args: list[str]) -> list[str]:
        return [a for a in args if not lexer.is_option(a)]

    # ================================================================ language commands
    @command("PRINT")
    def c_print(self, st: Stmt):
        fmt, cont, parts = "", False, []
        for kind, x in self._items(st):
            if kind == "fmt":
                name = _fmt_name(x)
                if name == "CONTinue":
                    cont = True
                elif name in PRINT_FMT:
                    fmt = PRINT_FMT[name]
            elif kind == "val":
                parts.append(plain(x, fmt))
        self.emit("".join(parts), cont)

    @command("PRINTF")
    def c_printf(self, st: Stmt):
        cont, vals = False, []
        for kind, x in self._items(st):
            if kind == "fmt" and _fmt_name(x) == "CONTinue":
                cont = True
            elif kind == "val":
                vals.append(x)
        if not vals or not isinstance(vals[0], Str):
            raise PracticeError("PRINTF: a format string expected")
        self.emit(printf_text(vals[0].v, vals[1:]), cont)

    @command("ECHO")
    def c_echo(self, st: Stmt):
        parts = [echo(x) for kind, x in self._items(st) if kind == "val"]
        self.emit("".join(parts))

    def c_declare(self, st: Stmt):
        names = [a.lstrip("&") for a in st.args if a.startswith("&")]
        for n in names:
            if st.canon == "GLOBAL":
                self.globals.setdefault(n, "")
            elif st.canon == "LOCAL":
                self.frame.locals[n] = ""
            else:
                self.frame.privates[n] = ""

    def _take(self, st: Stmt, values: list[str], private: bool):
        args = list(st.args)
        line_mode = False
        if args and args[0].startswith("%") and _fmt_name(args[0]) == "LINE":
            line_mode, args = True, args[1:]
        names = [a.lstrip("&") for a in args if a.startswith("&")]
        if line_mode and names:
            values = [" ".join(values)]
        level = self.frame.call_level()
        for k, n in enumerate(names):
            v = values[k] if k < len(values) else ""
            if private:
                own = self._declared_here(n)
                (own if own is not None else level.privates)[n] = v
            elif self._lookup(n):
                self.set_macro(n, v)
            else:
                level.locals[n] = v

    def _declared_here(self, name: str) -> dict | None:
        """The LOCAL/PRIVATE dict of the current call level (or its blocks) that declares name."""
        f = self.frame
        while f is not None:
            for d in (f.locals, f.privates):
                if name in d:
                    return d
            if f.kind != "block":
                return None
            f = f.parent
        return None

    def c_entry(self, st: Stmt):
        self._take(st, self.frame.call_level().entry_args, private=False)

    def c_enter(self, st: Stmt):
        # TRACE32 reads one line typed into the AREA window; cmmrun reads a line from stdin and
        # splits it into the macros like ENTRY does. No input left is an error, not empty
        # values, so a prompt loop (ENTER ... GOTO back) cannot spin forever.
        line = self.stdin.readline() if self.stdin is not None else ""
        if not line:
            raise PracticeError("ENTER: no input (stdin is closed or empty)")
        self._take(st, lexer.split_args(line.strip()), private=False)

    def c_parameters(self, st: Stmt):
        self._take(st, [_unquote(a) for a in self.frame.call_level().entry_args], private=True)

    def c_returnvalues(self, st: Stmt):
        vals = self.frame.call_level().entry_args
        for v in vals:
            if not (len(v) >= 2 and v[0] == '"' and v[-1] == '"'):
                raise PracticeError("Only string literals allowed here")
        # Like ENTRY: updates a visible macro (the simulator shows this), else creates one.
        self._take(st, [_unquote(a) for a in vals], private=False)

    def _call_args(self, args: list[str]) -> list[str]:
        return lexer.split_args(self.substitute(" ".join(args)))

    @command("GOSUB")
    def c_gosub(self, st: Stmt):
        if not st.args:
            raise PracticeError("GOSUB: label expected")
        prog = self.frame.prog
        vals = self._gosub(prog, st.args[0], self._call_args(st.args[1:]))
        self.frame.call_level().entry_args = vals

    @command("RETURN")
    def c_return(self, st: Stmt):
        if self.frame.call_level().kind != "gosub":
            raise PracticeError("#pra_stackun: PRACTICE stack underflow (RETURN outside a subroutine)")
        raise Return(self._call_args(st.args))

    @command("GOTO", "JUMPTO")
    def c_goto(self, st: Stmt):
        if not st.args:
            raise PracticeError("GOTO: label expected")
        prog = self.frame.prog
        idx = prog.labels.get(st.args[0])
        if idx is None:
            raise PracticeError(f"label not found: {st.args[0]}")
        raise Goto(idx, self.frame.call_level())

    @command("DO", "ChDir.DO")
    def c_do(self, st: Stmt):
        args = self._raw_args(st.args)
        if not args:
            raise PracticeError("DO: script expected")
        path = self.resolve_script(args[0], self.frame)
        if st.canon == "ChDir.DO":
            self.cwd = path.parent
        vals = self._do(path, self._call_args(args[1:]), self.frame)
        self.frame.call_level().entry_args = vals

    @command("RUN")
    def c_run(self, st: Stmt):
        args = self._raw_args(st.args)
        if not args:
            raise PracticeError("RUN: script expected")
        raise _RunRequest(self.resolve_script(args[0], self.frame), self._call_args(args[1:]))

    @command("ENDDO")
    def c_enddo(self, st: Stmt):
        raise EndDo(self._call_args(st.args))

    @command("END")
    def c_end(self, st: Stmt):
        raise EndAll("END")

    @command("QUIT")
    def c_quit(self, st: Stmt):
        """QUIT [code]: TRACE32 closes, and the code (default 0) is its process exit code."""
        text = self.substitute(" ".join(st.args)).strip()
        self.quit_code = to_int(self.eval(text), "exit code") & 0xFF if text else 0
        raise EndAll("END")

    @command("STOP")
    def c_stop(self, st: Stmt):
        raise EndAll("STOP: script stopped (TRACE32 would wait for CONTinue)")

    @command("CONTinue")
    def c_continue(self, st: Stmt):
        pass

    @command("SUBROUTINE")
    def c_subroutine(self, st: Stmt):
        pass

    @command("ON")
    def c_on(self, st: Stmt):
        args = self._raw_args(st.args)
        if not args or args[0].upper() != "ERROR":
            raise UnsupportedError(f"ON {args[0] if args else ''}: only ON ERROR is supported")
        rest = [a.upper() for a in args[1:]]
        level = self.frame.call_level()
        if not rest or rest[0] in ("INHERIT",):
            level.on_error = None
        elif rest[0].startswith("CONT"):
            level.on_error = ("CONTINUE", "")
        elif rest[0].startswith("DEF"):
            level.on_error = ("DEFAULT", "")
        elif rest[0] in ("GOTO", "GOSUB", "DO", "JUMPTO") and len(args) > 2:
            level.on_error = ("GOTO" if rest[0] == "JUMPTO" else rest[0], args[2])
        else:
            raise UnsupportedError("ON ERROR " + " ".join(args[1:]) + " is not supported")

    @command("WAIT")
    def c_wait(self, st: Stmt):
        times, cond = [], []
        for a in self._raw_args(st.args):
            toks = tokenize(a)
            if len(toks) == 2 and toks[0].kind == "num" and isinstance(toks[0].value, Time):
                times.append(toks[0].value.s)
            else:
                cond.append(a)
        limit = float(times[0]) if times else None
        start = self.backend.now()
        if not cond:
            while True:                 # sleep in slices so --timeout still applies
                self._check_time()
                left = (limit or 0) - (self.backend.now() - start)
                if left <= 0:
                    return
                self.backend.sleep(min(0.1, left))
        text = " ".join(cond)
        while not truth(self.eval(text)):
            self._check_time()
            left = None if limit is None else limit - (self.backend.now() - start)
            if left is not None and left <= 0:
                return          # TRACE32 continues after the timeout; scripts test the condition again
            self.backend.sleep(min(0.01, left) if left is not None else 0.01)

    @command("OPEN")
    def c_open(self, st: Stmt):
        args = [a for a in st.args]
        opts = {a.upper() for a in args if lexer.is_option(a)}
        pos = [a for a in args if not lexer.is_option(a)]
        if len(pos) < 2:
            raise PracticeError("OPEN #<n> <file> expected")
        mode = "r"
        if any(o.startswith(("/W", "/C")) for o in opts):
            mode = "w"
        if any(o.startswith("/A") for o in opts):
            mode = "a"
        if "/READ" in opts or "/R" in opts:
            mode = "r"
        self.files.open(pos[0].lstrip("#"), self.resolve_host_path(pos[1]), mode)

    @command("CLOSE")
    def c_close(self, st: Stmt):
        for a in st.args:
            self.files.close(a.lstrip("#"))

    @command("READ")
    def c_read(self, st: Stmt):
        if not st.args:
            raise PracticeError("READ #<n> expected")
        ch = st.args[0].lstrip("#")
        rest = st.args[1:]
        line_mode = bool(rest) and rest[0].startswith("%") and _fmt_name(rest[0]) == "LINE"
        names = [a.lstrip("&") for a in rest if a.startswith("&")]
        line = self.files.readline(ch)
        vals = [line] if line_mode else line.split()
        for k, n in enumerate(names):
            v = vals[k] if k < len(vals) else ""
            if self._lookup(n):
                self.set_macro(n, v)
            else:
                self.frame.call_level().locals[n] = v

    def _text_of(self, args: list[str]) -> str:
        parts = []
        for kind, x in self.ev.items(" ".join(args)):
            if kind == "val":
                parts.append(plain(x))
        return "".join(parts)

    @command("WRITE")
    def c_write(self, st: Stmt):
        if not st.args:
            raise PracticeError("WRITE #<n> expected")
        self.files.write(st.args[0].lstrip("#"), self._text_of(st.args[1:]))

    # STOre items that only hold GUI state, which cmmrun ignores; restoring them is a no-op.
    STORE_GUI_ITEMS = {"WIN", "HISTORY"}

    @command("STOre")
    def c_store(self, st: Stmt):
        """STOre <file> <item>...: TRACE32 writes a script that restores the items (a dated
        comment, B::, the commands, ENDDO). cmmrun writes the same frame for GUI-only items, so a
        later DO of the file works; items with debugger state (SYStem, Break, ...) are refused."""
        args = [a for a in st.args if not lexer.is_option(a)]
        if len(args) < 2:
            raise PracticeError("STOre <file> <item> expected")
        items = self.substitute(" ".join(args[1:])).split()
        other = [i for i in items if i.upper() not in self.STORE_GUI_ITEMS]
        if other:
            raise UnsupportedError(f"STOre {' '.join(other)}: only the GUI items Win and HISTory "
                                   "are supported (their state is not kept by cmmrun)")
        p = self.resolve_host_path(args[0])
        with open(p, "w", encoding="cp1252", errors="replace", newline="\n") as f:
            f.write(f"// cmmrun {time.strftime('%a %b %d %H:%M:%S %Y')}\n\nB::\n\n"
                    f"; {' '.join(items)}: GUI state, not kept by cmmrun\n\nENDDO\n")

    @command("APPEND")
    def c_append(self, st: Stmt):
        if not st.args:
            raise PracticeError("APPEND <file> expected")
        p = self.resolve_host_path(st.args[0])
        with open(p, "a", encoding="cp1252", errors="replace", newline="\n") as f:
            f.write(self._text_of(st.args[1:]) + "\n")

    @command("CD", "ChDir")
    def c_cd(self, st: Stmt):
        if st.args:
            p = self.resolve_host_path(st.args[0])
            if not p.is_dir():
                raise PracticeError(f"directory not found: {st.args[0]}")
            self.cwd = p

    # ================================================================ symbols
    @command("sYmbol.CREATE.Label")
    def c_symcreate(self, st: Stmt):
        args = self._raw_args(st.args)
        if len(args) < 2:
            raise PracticeError("sYmbol.CREATE.Label <name> <address> expected")
        text = " ".join(args[1:])
        if HEX_RE.fullmatch(text):                 # the common generated form: one hex literal
            a = Addr("C", int(text[2:].replace("_", ""), 16))
        else:
            v = self.ev.eval(text)
            a = v if isinstance(v, Addr) and v.access else Addr("C", to_int(v, "address"))
        self.new_symbols[args[0]] = a

    @command("sYmbol.CREATE.Done")
    def c_symdone(self, st: Stmt):
        self.symbols.update(self.new_symbols)
        self.new_symbols.clear()

    @command("sYmbol.RESet")
    def c_symreset(self, st: Stmt):
        self.symbols.clear()
        self.new_symbols.clear()

    # ================================================================ target commands
    @command("SYStem.CPU", target=True)
    def c_cpu(self, st: Stmt):
        if st.args:
            self.backend.set_cpu(st.args[0])

    @command("SYStem.JtagClock", target=True)
    def c_clock(self, st: Stmt):
        if st.args:
            self.backend.set_option("clock", " ".join(self._raw_args(st.args)))

    @command("SYStem.CONFIG.DEBUGPORTTYPE", target=True)
    def c_debugport(self, st: Stmt):
        a = self._raw_args(st.args)
        port = a[0].upper() if a else ""
        if port not in ("SWD", "JTAG"):
            raise UnsupportedError(f"SYStem.CONFIG.DEBUGPORTTYPE {port or '?'}: only SWD and JTAG are supported")
        self.backend.set_option("interface", port.lower())

    @command("SYStem.Up", "SYStem.Mode.Up", target=True)
    def c_up(self, st: Stmt):
        self.backend.connect("up")
        self.backend.reset(halt=True)

    @command("SYStem.Attach", "SYStem.Mode.Attach", target=True)
    def c_attach(self, st: Stmt):
        self.backend.connect("attach")

    @command("SYStem.Mode.Prepare", target=True)
    def c_prepare(self, st: Stmt):
        self.backend.connect("prepare")

    @command("SYStem.Mode.Go", target=True)
    def c_sysgo(self, st: Stmt):
        self.backend.connect("up")
        self.backend.reset(halt=False)

    @command("SYStem.Down", "SYStem.Mode.Down", "SYStem.Mode.NoDebug", target=True)
    def c_down(self, st: Stmt):
        if self.backend.connected:
            self.backend.disconnect()

    @command("SYStem.RESet", target=True)
    def c_sysreset(self, st: Stmt):
        if self.backend.connected:
            self.backend.disconnect()
        self.backend.options.clear()

    @command("RESet", target=True)
    def c_reset(self, st: Stmt):
        if self.backend.connected:
            self.backend.disconnect()

    @command("Register.RESet", connected=True)
    def c_regreset(self, st: Stmt):
        self.backend.reset(halt=True)

    @command("Register.Set", connected=True)
    def c_regset(self, st: Stmt):
        args = self._raw_args(st.args)
        if len(args) < 2:
            raise PracticeError("Register.Set <register> <value> expected")
        self.backend.write_reg(args[0], to_int(self.eval(" ".join(args[1:])), "register value"))

    @command("Go", connected=True)
    def c_go(self, st: Stmt):
        args = self._raw_args(st.args)
        until = to_int(self.eval(" ".join(args)), "address") if args else None
        self.backend.go(until)

    @command("Break", connected=True)
    def c_break(self, st: Stmt):
        self.backend.halt()

    @command("Step", connected=True)
    def c_step(self, st: Stmt):
        args = self._raw_args(st.args)
        self.backend.step(to_int(self.eval(" ".join(args))) if args else 1)

    @command("Data.Set", connected=True)
    def c_dataset(self, st: Stmt):
        items = self._items(st)
        if not items or items[0][0] != "val":
            raise PracticeError("Data.Set <address> <values> expected")
        where = items[0][1]
        size, order, verify = 1, "big" if self.big_endian else "little", False
        payload = bytearray()
        sized_seen = False
        for kind, x in items[1:]:
            if kind == "fmt":
                name = _fmt_name(x)
                if name in SIZES:
                    size, sized_seen = SIZES[name], True
                elif name == "LE":
                    order = "little"
                elif name == "BE":
                    order = "big"
            elif kind == "opt":
                if x.upper().startswith("/V"):
                    verify = True
            elif isinstance(x, Str):
                payload += x.v.encode("latin-1", errors="replace")
            elif isinstance(x, Num) and x.radix == "char" and not sized_seen:
                payload.append(x.v & 0xFF)
            else:
                payload += (to_int(x) & ((1 << (8 * size)) - 1)).to_bytes(size, order)
        if not payload:
            raise PracticeError("Data.Set: no value")
        if isinstance(where, Rng):
            access, lo, n = where.lo.access, where.lo.v, where.hi - where.lo.v + 1
            if n <= 0:
                raise PracticeError(f"Data.Set: empty address range 0x{where.lo.v:X}--0x{where.hi:X}")
            data = (bytes(payload) * (n // len(payload) + 1))[:n]
        else:
            access = where.access if isinstance(where, Addr) else ""
            lo, data = to_int(where, "address"), bytes(payload)
        self.backend.write(access, lo, data)
        if verify and self.backend.read(access, lo, len(data)) != data:
            raise TargetError(f"Data.Set /Verify failed at 0x{lo:X}")

    @command("PER.Set.Field", connected=True)
    def c_per_set_field(self, st: Stmt):
        """PER.Set.Field <address> %<size> <mask> <value>: read, change the masked bits, write.
        The value is shifted up to the mask's lowest set bit, as in TRACE32 (simulator: mask
        0xFF00 value 0xAB turns 0x12345678 into 0x1234AB78)."""
        items = self._items(st)
        if any(k == "opt" for k, _ in items):
            raise PracticeError("PER.Set.Field: too many keywords in line")     # as TRACE32
        size, order, vals = None, "big" if self.big_endian else "little", []
        for kind, x in items:
            if kind == "fmt":
                name = _fmt_name(x)
                if name in SIZES:
                    size = SIZES[name]
                elif name in ("LE", "BE"):
                    order = "little" if name == "LE" else "big"
            elif kind == "val":
                vals.append(x)
        if size is None or len(vals) != 3:
            # TRACE32 silently does nothing for these; refuse instead of skipping the write
            raise UnsupportedError("PER.Set.Field: only <address> %<size> <mask> <value> is supported")
        where, mask, value = vals[0], to_int(vals[1], "mask"), to_int(vals[2], "value")
        if isinstance(where, Rng):
            raise PracticeError("PER.Set.Field: an address expected, not a range")
        access = where.access if isinstance(where, Addr) else ""
        addr = to_int(where, "address")
        full = (1 << (8 * size)) - 1
        mask &= full
        if not mask:
            return                                    # no bits to change (TRACE32: no change)
        shift = (mask & -mask).bit_length() - 1
        old = int.from_bytes(self.backend.read(access, addr, size), order)
        new = (old & ~mask & full) | ((value << shift) & mask)
        self.backend.write(access, addr, new.to_bytes(size, order))

    def _load(self, st: Stmt, kind: str):
        pos = self._raw_args(st.args)
        opts = [a.upper() for a in st.args if lexer.is_option(a)]
        if not pos:
            raise PracticeError(f"{st.canon}: file expected")
        path = self.resolve_host_path(pos[0])
        if not path.is_file():
            raise PracticeError(f"file not found: {pos[0]}")
        if any(o.startswith("/NOCODE") or o == "/NC" for o in opts):
            return
        offset = 0
        if len(pos) > 1:
            v = self.eval(" ".join(pos[1:]))
            offset = v.lo.v if isinstance(v, Rng) else to_int(v, "address")
        segs = load_image(path, kind, offset)
        for addr, data in segs:
            self.backend.write("", addr, data)
        self.backend.log("load", file=path.name, kind=kind, segments=len(segs),
                         bytes=sum(len(d) for _, d in segs))

    @command("Data.LOAD.Elf", connected=True)
    def c_load_elf(self, st):
        self._load(st, "elf")
        self.elf_loaded = True

    @command("Data.LOAD.Binary", connected=True)
    def c_load_bin(self, st):
        self._load(st, "binary")

    @command("Data.LOAD.Ihex", "Data.LOAD.IntelHex", connected=True)
    def c_load_hex(self, st):
        self._load(st, "ihex")

    @command("Data.LOAD.Srecord", "Data.LOAD.S3record", connected=True)
    def c_load_srec(self, st):
        self._load(st, "srecord")

    @command("Data.SAVE.Binary", connected=True)
    def c_save_bin(self, st: Stmt):
        pos = self._raw_args(st.args)
        if len(pos) < 2:
            raise PracticeError("Data.SAVE.Binary <file> <range> expected")
        r = self.eval(" ".join(pos[1:]))
        if not isinstance(r, Rng):
            raise PracticeError("Data.SAVE.Binary: address range expected")
        data = self.backend.read(r.lo.access, r.lo.v, r.hi - r.lo.v + 1)
        self.resolve_host_path(pos[0]).write_bytes(data)

    @command("FLASH.TARGET", "FLASH.CLEAR", target=True)
    def c_flash_cfg(self, st: Stmt):
        # the J-Link DLL brings its own flash loader: TRACE32's flash algorithm setup is not needed
        self.backend.log("flash_config", cmd=st.canon)

    @command("FLASH.RESet", target=True)
    def c_flash_reset(self, st: Stmt):
        self.backend.flash_clear_ranges()
        self.backend.log("flash_config", cmd=st.canon)

    def _flash_range(self, args: list[str]) -> Rng | None:
        for a in args[:2]:          # FLASH.Create <unit> <range> ...  or  FLASH.Erase <range>
            try:
                r = self.eval(a)
            except PracticeError:
                continue
            if isinstance(r, Rng):
                return r
        return None

    @command("FLASH.Create", target=True)
    def c_flash_create(self, st: Stmt):
        r = self._flash_range(self._raw_args(st.args))
        if r is None:
            raise PracticeError("FLASH.Create: address range expected, e.g. FLASH.Create 1. 0x0--0xFFFF ...")
        self.backend.flash_add_range(r.lo.v, r.hi)

    @command("FLASH.ReProgram", "FLASH.Program", "FLASH.AUTO", target=True)
    def c_flash_reprogram(self, st: Stmt):
        # all three collect writes to flash and program them when switched OFF
        a = [x.upper() for x in self._raw_args(st.args)]
        if "OFF" in a:
            self.backend.flash_end()
        else:
            self._need_target(st.canon)
            self.backend.flash_begin()

    @command("FLASH.Erase", connected=True)
    def c_flash_erase(self, st: Stmt):
        a = self._raw_args(st.args)
        if not a or a[0].upper() == "ALL":
            self.backend.flash_erase()
            return
        r = self._flash_range(a)
        if r is None:
            raise PracticeError("FLASH.Erase: ALL or an address range expected")
        self.backend.flash_erase(r.lo.v, r.hi)


class _RunRequest(Exception):
    def __init__(self, path, args):
        self.path, self.args = path, args


def _unquote(a: str) -> str:
    if len(a) >= 2 and a[0] == '"' and a[-1] == '"':
        return a[1:-1].replace('""', '"')
    return a

