"""PRACTICE expressions: tokenizer and evaluator.

Macros are already replaced in the text before it gets here. Numbers that start
with a digit are hex unless they end in a dot (`10.`) or have a fraction
(`1.5`). `0x` is hex, `0y` binary. `500.ms`, `1.s`, `100us` are times.
`SD:0x1000` is an address with an access class. `a--b` and `a++n` are ranges.

A command's arguments are a *sequence* of expressions (`PRINT "a" 1+2 "b"`),
mixed with format specifiers (`%Long`, `%Decimal`) and options (`/Verify`).
`parse_items` returns that sequence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable

from .values import (Addr, Bool, Flt, Num, PracticeError, Rng, Str, Time, UnsupportedError, Value,
                     to_int, truth)

TIME_UNITS = {"ns": Fraction(1, 10**9), "us": Fraction(1, 10**6), "ms": Fraction(1, 1000), "s": Fraction(1),
              "ks": Fraction(1000)}
OPS = ["<<", ">>", "<=", ">=", "==", "!=", "&&", "||", "^^", "--", "++", "..",
       "+", "-", "*", "/", "%", "&", "|", "^", "<", ">", "!", "~", "(", ")", ",", ":"]
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
NUM_RE = re.compile(r"0[xX][0-9A-Fa-f]+|0[yY][01]+|[0-9][0-9A-Fa-f]*(?:\.[0-9]*)?")


@dataclass
class Tok:
    kind: str      # num str char ident op fmt opt end
    text: str
    pos: int
    value: Value | None = None
    space_before: bool = False


def _number(text: str) -> Value:
    t = text
    if t[:2] in ("0x", "0X"):
        return Num(int(t[2:], 16), "hex")
    if t[:2] in ("0y", "0Y"):
        return Num(int(t[2:], 2), "bin")
    if "." in t:
        ip, fp = t.split(".", 1)
        if not re.fullmatch(r"[0-9]+", ip):
            raise PracticeError(f"bad number {t!r}")
        if fp == "":
            return Num(int(ip), "dec")
        return Flt(Fraction(f"{ip}.{fp}"))
    return Num(int(t, 16), "hex")


def tokenize(s: str) -> list[Tok]:
    out: list[Tok] = []
    i, n = 0, len(s)
    pending_space = False
    while i < n:
        c = s[i]
        if c in " \t":
            while i < n and s[i] in " \t":
                i += 1
            pending_space = True
            continue
        sp, pending_space = pending_space and bool(out), False
        if c == '"':
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise PracticeError("string not terminated")
                if s[j] == '"':
                    if j + 1 < n and s[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            out.append(Tok("str", s[i:j + 1], i, Str("".join(buf)), sp))
            i = j + 1
            continue
        if c == "'" and i + 2 < n and s[i + 2] == "'":
            out.append(Tok("char", s[i:i + 3], i, Num(ord(s[i + 1]), "char"), sp))
            i += 3
            continue
        if c.isdigit():
            m = NUM_RE.match(s, i)
            t = m.group(0)
            j = m.end()
            # A number directly followed by a time unit is a time.
            unit = next((u for u in ("ns", "us", "ms", "ks", "s") if s.startswith(u, j)
                         and not (j + len(u) < n and (s[j + len(u)].isalnum() or s[j + len(u)] == "_"))), None)
            if unit and not t.lower().startswith(("0x", "0y")):
                num = t.rstrip(".")
                out.append(Tok("num", s[i:j + len(unit)], i, Time(Fraction(num) * TIME_UNITS[unit]), sp))
                i = j + len(unit)
                continue
            if j < n and (s[j].isalpha() or s[j] == "_"):
                raise PracticeError(f"bad number {s[i:j + 1]}...")
            out.append(Tok("num", t, i, _number(t), sp))
            i = j
            continue
        if c.isalpha() or c == "_":
            m = IDENT_RE.match(s, i)
            out.append(Tok("ident", m.group(0), i, None, sp))
            i = m.end()
            continue
        if c == "%" and i + 1 < n and s[i + 1].isalpha() and (not out or sp or out[-1].text in ("(", ",")):
            m = IDENT_RE.match(s, i + 1)
            out.append(Tok("fmt", "%" + m.group(0), i, None, sp))
            i = m.end()
            continue
        if c == "/" and i + 1 < n and (s[i + 1].isalpha() or s[i + 1] == "&") and (sp or not out):
            m = IDENT_RE.match(s, i + 1)
            if m:
                out.append(Tok("opt", "/" + m.group(0), i, None, sp))
                i = m.end()
                continue
        op = next((o for o in OPS if s.startswith(o, i)), None)
        if op is None:
            raise PracticeError(f"unexpected character {c!r} in {s!r}")
        out.append(Tok("op", op, i, None, sp))
        i += len(op)
    out.append(Tok("end", "", n))
    return out


# Binary operators, lowest precedence first.
LEVELS = [["--", "++", ".."], ["||"], ["^^"], ["&&"], ["|"], ["^"], ["&"], ["==", "!="],
          ["<", "<=", ">", ">="], ["<<", ">>"], ["+", "-"], ["*", "/", "%"]]
BINARY = {op: lvl for lvl, ops in enumerate(LEVELS) for op in ops}


class Evaluator:
    """Evaluates token streams. `call(name, args_text_list, arg_values_thunk)` runs functions."""

    def __init__(self, call: Callable[[str, list[str], "Evaluator"], Value]):
        self.call = call
        self.toks: list[Tok] = []
        self.i = 0
        self.src = ""

    # ---- entry points
    def eval(self, text: str) -> Value:
        self._start(text)
        v = self._expr(0)
        if self._peek().kind != "end":
            raise PracticeError(f"unexpected {self._peek().text!r} in expression {text!r}")
        return v

    def items(self, text: str, keep_words: bool = False) -> list[tuple[str, object]]:
        """Sequence of ('val', Value) / ('fmt', '%Long') / ('opt', '/Verify') / ('word', 'PC')."""
        self._start(text)
        out = []
        while self._peek().kind != "end":
            t = self._peek()
            if t.kind == "fmt":
                self.i += 1
                out.append(("fmt", t.text))
            elif t.kind == "opt":
                self.i += 1
                out.append(("opt", t.text))
            elif keep_words and t.kind == "ident" and self._peek(1).text not in ("(", ":"):
                self.i += 1
                out.append(("word", t.text))
            else:
                out.append(("val", self._expr(0)))
        return out

    # ---- internals
    def _start(self, text):
        self.src = text
        self.toks = tokenize(text)
        self.i = 0

    def _peek(self, k=0) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def _expr(self, level: int) -> Value:
        if level >= len(LEVELS):
            return self._unary()
        left = self._expr(level + 1)
        while True:
            t = self._peek()
            if t.kind == "op" and t.text in LEVELS[level]:
                # `a -1` after a space, in an argument list, is two values: stop.
                if t.text in ("-", "+") and t.space_before and self._peek(1).kind != "end" \
                        and not self._peek(1).space_before:
                    return left
                self.i += 1
                right = self._expr(level + 1)
                left = binop(t.text, left, right)
            else:
                return left

    def _unary(self) -> Value:
        t = self._peek()
        if t.kind == "op" and t.text in ("-", "!", "~", "+"):
            self.i += 1
            v = self._unary()
            if t.text == "-":
                if isinstance(v, Flt):
                    return Flt(-v.v)
                if isinstance(v, Time):
                    return Time(-v.s)
                return Num(-to_int(v), v.radix if isinstance(v, Num) else "hex")
            if t.text == "!":
                return Bool(not truth(v))
            if t.text == "~":
                return Num(~to_int(v), v.radix if isinstance(v, Num) else "hex")
            return v
        return self._primary()

    def _primary(self) -> Value:
        t = self._peek()
        if t.kind in ("num", "str", "char"):
            self.i += 1
            return t.value
        if t.kind == "op" and t.text == "(":
            self.i += 1
            v = self._expr(0)
            self._expect(")")
            return v
        if t.kind == "ident":
            nxt = self._peek(1)
            if nxt.kind == "op" and nxt.text == "(" and not nxt.space_before:
                self.i += 2
                args = self._call_args()
                return self.call(t.text, args, self)
            if nxt.kind == "op" and nxt.text == ":" and not nxt.space_before and len(t.text) <= 8 \
                    and "." not in t.text:
                self.i += 2
                v = self._unary()
                return Addr(t.text.upper(), to_int(v, "address"))
            raise UnsupportedError(f"symbol {t.text!r}: debug symbols are not supported (use an address)")
        if t.kind == "end":
            raise PracticeError(f"expression expected in {self.src!r}")
        raise PracticeError(f"unexpected {t.text!r} in {self.src!r}")

    def _expect(self, op):
        t = self._peek()
        if t.kind != "op" or t.text != op:
            raise PracticeError(f"expected {op!r} in {self.src!r}")
        self.i += 1

    def _call_args(self) -> list[str]:
        """Raw text of each argument (functions decide how to evaluate them)."""
        args, depth, start = [], 0, self.i
        while True:
            t = self._peek()
            if t.kind == "end":
                raise PracticeError(f"missing ')' in {self.src!r}")
            if t.kind == "op" and t.text == "(":
                depth += 1
            elif t.kind == "op" and t.text == ")":
                if depth == 0:
                    end = t.pos
                    seg = self.src[self.toks[start].pos:end] if self.i > start else ""
                    if seg.strip() or args:
                        args.append(seg.strip())
                    self.i += 1
                    return args
                depth -= 1
            elif t.kind == "op" and t.text == "," and depth == 0:
                args.append(self.src[self.toks[start].pos:t.pos].strip())
                self.i += 1
                start = self.i
                continue
            self.i += 1


def sub_eval(ev: Evaluator, text: str) -> Value:
    """Evaluate a function argument without disturbing the caller's position."""
    saved = (ev.toks, ev.i, ev.src)
    try:
        return ev.eval(text)
    finally:
        ev.toks, ev.i, ev.src = saved


def _radix(a: Num, b: Num) -> str:
    return "dec" if "dec" in (a.radix, b.radix) else "hex"


def _cmp(op, a, b):
    return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def binop(op: str, a: Value, b: Value) -> Value:
    if op in ("&&", "||", "^^"):
        x, y = truth(a), truth(b)
        return Bool(x and y if op == "&&" else x or y if op == "||" else x != y)
    if op in ("--", "++", ".."):
        lo = a if isinstance(a, Addr) else Addr("", to_int(a))
        hi = to_int(b)
        return Rng(lo, lo.v + hi if op == "++" else hi)
    if isinstance(a, Str) or isinstance(b, Str):
        if isinstance(a, Str) and isinstance(b, Str):
            if op == "+":
                return Str(a.v + b.v)
            if op in ("==", "!=", "<", "<=", ">", ">="):
                return Bool(_cmp(op, a.v, b.v))
        raise PracticeError(f"operator {op!r} needs two numbers or two strings")
    if isinstance(a, Bool) and isinstance(b, Bool) and op in ("==", "!="):
        return Bool(_cmp(op, a.v, b.v))
    if isinstance(a, Time) or isinstance(b, Time):
        x = a.s if isinstance(a, Time) else Fraction(to_int(a))
        y = b.s if isinstance(b, Time) else Fraction(to_int(b))
        if op in ("==", "!=", "<", "<=", ">", ">="):
            return Bool(_cmp(op, x, y))
        if op in ("+", "-"):
            return Time(x + y if op == "+" else x - y)
        if op == "*":
            return Time(x * y)
        if op == "/" and isinstance(b, Time):
            return Flt(x / y)
        if op == "/":
            return Time(x / y)
        raise PracticeError(f"operator {op!r} not allowed on times")
    if isinstance(a, Flt) or isinstance(b, Flt):
        x = a.v if isinstance(a, Flt) else Fraction(to_int(a))
        y = b.v if isinstance(b, Flt) else Fraction(to_int(b))
        if op in ("==", "!=", "<", "<=", ">", ">="):
            return Bool(_cmp(op, x, y))
        if op == "/" and y == 0:
            raise PracticeError("division by zero")
        r = {"+": lambda: x + y, "-": lambda: x - y, "*": lambda: x * y, "/": lambda: x / y}.get(op)
        if r is None:
            raise PracticeError(f"operator {op!r} not allowed on floats")
        return Flt(r())
    if isinstance(a, Addr):
        y = to_int(b)
        if op in ("+", "-"):
            return Addr(a.access, a.v + y if op == "+" else a.v - y)
        if op in ("==", "!=", "<", "<=", ">", ">="):
            return Bool(_cmp(op, a.v, y))
        a = Num(a.v)
    x, y = to_int(a), to_int(b)
    ra = a if isinstance(a, Num) else Num(x)
    rb = b if isinstance(b, Num) else Num(y)
    rad = _radix(ra, rb)
    if op in ("==", "!=", "<", "<=", ">", ">="):
        return Bool(_cmp(op, x, y))
    if op in ("/", "%") and y == 0:
        raise PracticeError("division by zero")
    if op == "/":
        q = abs(x) // abs(y)
        return Num(q if (x >= 0) == (y >= 0) else -q, rad)
    if op == "%":
        r = abs(x) % abs(y)
        return Num(r if x >= 0 else -r, rad)
    f = {"+": x + y, "-": x - y, "*": x * y, "&": x & y, "|": x | y, "^": x ^ y,
         "<<": x << y if 0 <= y < 128 else 0, ">>": x >> y if y >= 0 else 0}
    return Num(f[op], rad)
