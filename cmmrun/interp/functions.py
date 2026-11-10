"""PRACTICE functions that cmmrun implements.

Each function receives the raw text of its arguments and decides how to
evaluate them (`Register(PC)` takes a register name, not an expression).
Names are canonical TRACE32 names; the interpreter resolves abbreviations
(`STR.LEN`, `OS.PPD`) through the vocabulary before looking them up here.

`REGISTRY` is the single source of truth for `check` and the run pre-flight:
a function is supported exactly when it is in this table.
"""
from __future__ import annotations

import fnmatch
import os
import pathlib
from fractions import Fraction
from typing import TYPE_CHECKING, Callable

from .expr import Evaluator, sub_eval
from .values import (Addr, Bool, Flt, Num, PracticeError, Str, Value, plain, to_int, to_str)

if TYPE_CHECKING:
    from .interpreter import Interpreter

Fn = Callable[["Interpreter", list[str], Evaluator], Value]
REGISTRY: dict[str, Fn] = {}
TARGET_FUNCTIONS: set[str] = set()      # need a connected target


def fn(*names: str, target: bool = False):
    def deco(f: Fn) -> Fn:
        for n in names:
            REGISTRY[n] = f
            if target:
                TARGET_FUNCTIONS.add(n)
        return f
    return deco


def _vals(ev: Evaluator, args: list[str], n: int | None = None, name: str = "") -> list[Value]:
    if n is not None and len(args) != n:
        raise PracticeError(f"{name}: expected {n} argument(s), got {len(args)}")
    return [sub_eval(ev, a) for a in args]


def _s(ev, a):
    return to_str(sub_eval(ev, a))


def _i(ev, a):
    return to_int(sub_eval(ev, a))


def _dec(v: int) -> Num:
    return Num(v, "dec")


# ---- constants
@fn("TRUE")
def _true(it, a, ev):
    return Bool(True)


@fn("FALSE")
def _false(it, a, ev):
    return Bool(False)


@fn("FOUND")
def _found(it, a, ev):
    return Bool(it.found)


# ---- strings
@fn("STRing.LENgth")
def _len(it, a, ev):
    return _dec(len(_s(ev, a[0])))


@fn("STRing.MID")
def _mid(it, a, ev):
    s, start, n = _s(ev, a[0]), _i(ev, a[1]), _i(ev, a[2])
    return Str(s[start:start + n] if start >= 0 else "")


@fn("STRing.CUT")
def _cut(it, a, ev):
    s, n = _s(ev, a[0]), _i(ev, a[1])
    return Str(s[n:] if n >= 0 else s[:len(s) + n])


@fn("STRing.TRIM")
def _trim(it, a, ev):
    return Str(_s(ev, a[0]).strip(" \t"))


@fn("STRing.UPpeR")
def _upper(it, a, ev):
    return Str(_s(ev, a[0]).upper())


@fn("STRing.LoWeR")
def _lower(it, a, ev):
    return Str(_s(ev, a[0]).lower())


@fn("STRing.SCAN")
def _scan(it, a, ev):
    s, sub, start = _s(ev, a[0]), _s(ev, a[1]), _i(ev, a[2])
    return Num(s.find(sub, max(0, start)))


@fn("STRing.FIND")
def _find(it, a, ev):
    s, chars = _s(ev, a[0]), _s(ev, a[1])
    return Bool(any(c in s for c in chars))


@fn("STRing.Replace")
def _replace(it, a, ev):
    s, old, new, count = _s(ev, a[0]), _s(ev, a[1]), _s(ev, a[2]), _i(ev, a[3])
    return Str(s.replace(old, new) if count == 0 else s.replace(old, new, count))


@fn("STRing.ComPare")
def _compare(it, a, ev):
    return Bool(fnmatch.fnmatchcase(_s(ev, a[0]), _s(ev, a[1])))


@fn("STRing.CHAR")
def _char(it, a, ev):
    s, i = _s(ev, a[0]), _i(ev, a[1])
    if not 0 <= i < len(s):
        raise PracticeError("STRing.CHAR: index out of range")
    return Num(ord(s[i]))


@fn("STRing.SPLIT")
def _split(it, a, ev):
    s, sep, i = _s(ev, a[0]), _s(ev, a[1]), _i(ev, a[2])
    parts = s.split(sep) if sep else [s]
    return Str(parts[i] if 0 <= i < len(parts) else "")


@fn("STRing.COUNT")
def _count(it, a, ev):
    return _dec(_s(ev, a[0]).count(_s(ev, a[1])))


@fn("STRing.SCANAndExtract")
def _scanx(it, a, ev):
    # The key must start a whitespace-separated word; the value is the rest of that word.
    s, key, default = _s(ev, a[0]), _s(ev, a[1]), _s(ev, a[2])
    for word in s.split():
        if word.startswith(key):
            return Str(word[len(key):])
    return Str(default)


# ---- formatting and conversion
@fn("FORMAT.HEX")
def _fhex(it, a, ev):
    w, v = _i(ev, a[0]), _i(ev, a[1])
    return Str(format(v & ((1 << 64) - 1), "X").rjust(w, "0"))


@fn("FORMAT.Decimal")
def _fdec(it, a, ev):
    w, v = _i(ev, a[0]), _i(ev, a[1])
    return Str(str(v).rjust(w))


@fn("FORMAT.DecimalU", "FORMAT.UDECIMAL")
def _fudec(it, a, ev):
    w, v = _i(ev, a[0]), _i(ev, a[1])
    return Str(str(v & ((1 << 64) - 1)).rjust(w))


@fn("FORMAT.BINary")
def _fbin(it, a, ev):
    w, v = _i(ev, a[0]), _i(ev, a[1])
    return Str(format(v & ((1 << 64) - 1), "b").rjust(w, "0"))


@fn("FORMAT.STRing")
def _fstr(it, a, ev):
    s, w, fill = _s(ev, a[0]), _i(ev, a[1]), sub_eval(ev, a[2])
    ch = chr(to_int(fill)) if isinstance(fill, Num) else to_str(fill)[:1] or " "
    return Str(s.ljust(w, ch) if w >= 0 else s.rjust(-w, ch))


@fn("CONVert.CHAR")
def _cchar(it, a, ev):
    return Str(chr(_i(ev, a[0]) & 0xFF))


@fn("CONVert.HEXTOINT")
def _hex2int(it, a, ev):
    # Takes a number (not a string) and gives it decimal radix: CONVert.HEXTOINT(0x1F) prints 31.
    return _dec(_i(ev, a[0]))


@fn("CONVert.INTTOHEX")
def _int2hex(it, a, ev):
    return Num(_i(ev, a[0]), "hex")


@fn("CONVert.BOOLTOINT")
def _b2i(it, a, ev):
    v = sub_eval(ev, a[0])
    return _dec(1 if (v.v if isinstance(v, Bool) else to_int(v)) else 0)


@fn("CONVert.INTTOFLOAT")
def _i2f(it, a, ev):
    return Flt(Fraction(_i(ev, a[0])))


@fn("CONVert.FLOATTOINT")
def _f2i(it, a, ev):
    v = sub_eval(ev, a[0])
    return _dec(int(v.v) if isinstance(v, Flt) else to_int(v))


# ---- host: files, directories, environment
@fn("OS.PresentPracticeDirectory")
def _ppd(it, a, ev):
    return Str(str(it.script_dir()))


@fn("OS.PresentWorkingDirectory")
def _pwd(it, a, ev):
    return Str(str(it.cwd))


@fn("OS.FILE", "OS.FILE.EXIST")
def _osfile(it, a, ev):
    return Bool(it.resolve_host_path(_s(ev, a[0]) if a[0].lstrip().startswith('"') else a[0].strip()).is_file())


@fn("OS.DIR")
def _osdir(it, a, ev):
    return Bool(it.resolve_host_path(_s(ev, a[0]) if a[0].lstrip().startswith('"') else a[0].strip()).is_dir())


@fn("OS.ENV")
def _env(it, a, ev):
    name = _s(ev, a[0]) if a[0].lstrip().startswith('"') else a[0].strip()
    return Str(os.environ.get(name, ""))


@fn("OS.FILE.NAME")
def _fname(it, a, ev):
    return Str(pathlib.PureWindowsPath(_s(ev, a[0])).name)


@fn("OS.FILE.PATH")
def _fpath(it, a, ev):
    return Str(str(pathlib.PureWindowsPath(_s(ev, a[0])).parent))


@fn("OS.FILE.EXTENSION")
def _fext(it, a, ev):
    return Str(pathlib.PureWindowsPath(_s(ev, a[0])).suffix)


@fn("FILE.EOF", "FILE.EOFLASTREAD")
def _eof(it, a, ev):
    return Bool(it.files.eof(a[0].strip().lstrip("#") if a else "1"))


@fn("PRACTICE.ARG.SIZE")
def _argsize(it, a, ev):
    return _dec(len(it.frame.call_level().entry_args))


@fn("PRACTICE.ARG")
def _arg(it, a, ev):
    args = it.frame.call_level().entry_args
    i = _i(ev, a[0])
    return Str(args[i] if 0 <= i < len(args) else "")


@fn("Var.STRing")
def _varstr(it, a, ev):
    raise PracticeError("Var.STRing needs debug symbols, which cmmrun does not support")


# ---- target
def _addr(ev, text) -> Addr:
    v = sub_eval(ev, text)
    return v if isinstance(v, Addr) else Addr("", to_int(v, "address"))


def _read(it, ev, text, size, signed=False) -> Value:
    ad = _addr(ev, text)
    data = it.backend.read(ad.access, ad.v, size)
    order = "big" if it.big_endian else "little"
    return Num(int.from_bytes(data, order, signed=signed))


for _name, _size in [("Data.Byte", 1), ("Data.Word", 2), ("Data.Short", 2), ("Data.Long", 4),
                     ("Data.Quad", 8)]:
    def _mk(size):
        def f(it, a, ev):
            if len(a) != 1:
                raise PracticeError("expected one address")
            return _read(it, ev, a[0], size)
        return f
    fn(_name, target=True)(_mk(_size))


@fn("Data.STRing", target=True)
def _dstr(it, a, ev):
    ad = _addr(ev, a[0])
    out = bytearray()
    while len(out) < 4096:
        b = it.backend.read(ad.access, ad.v + len(out), 1)[0]
        if b == 0:
            break
        out.append(b)
    return Str(out.decode("latin-1"))


@fn("Register", target=True)
def _reg(it, a, ev):
    if len(a) != 1:
        raise PracticeError("Register() takes one register name")
    return Num(it.backend.read_reg(a[0].strip()))


@fn("STATE.RUN", target=True)
def _run(it, a, ev):
    return Bool(it.backend.is_running())


@fn("STATE.HALT", target=True)
def _halt(it, a, ev):
    return Bool(not it.backend.is_running())


@fn("SYStem.Up", "SYStem.UP")
def _sysup(it, a, ev):
    return Bool(it.backend.connected)


def call(it: "Interpreter", canon: str, args: list[str], ev: Evaluator) -> Value:
    f = REGISTRY.get(canon)
    if f is None:
        from .values import UnsupportedError
        raise UnsupportedError(f"function {canon}() is not supported by cmmrun")
    try:
        return f(it, args, ev)
    except IndexError:
        raise PracticeError(f"{canon}(): wrong number of arguments") from None


def describe(v: Value) -> str:
    return plain(v)
