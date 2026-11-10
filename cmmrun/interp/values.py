"""PRACTICE values and how they print.

Every rule here was checked against the TRACE32 simulator (see tests/cmm/*.expected):

  - Integers remember their radix. A literal is hex unless it ends in a dot
    (`10.`). If either operand of an arithmetic operator is decimal, the
    result is decimal; otherwise it is hex.
  - PRINT shows hex in upper case without `0x`, and puts a `0` in front when
    the first digit is a letter (`0FF`, `0AB`, `1F`). Negative numbers are
    shown as 64-bit two's complement (`0FFFFFFFFFFFFFFFF`).
  - ECHO and macro assignment use the "decorated" form: `0x1F`, `10.`,
    `TRUE()`. A macro stores a single letter digit as `0x0A`.
  - Booleans PRINT as `TRUE ` (with a trailing space) and `FALSE`.
  - Floats keep 18 significant digits (`33.3333333333333333`); a whole float
    prints with one decimal (`5.0`). We compute them exactly as fractions.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

MASK64 = (1 << 64) - 1


class PracticeError(Exception):
    """An error in the script (exit code 2)."""


class TargetError(Exception):
    """An error reported by the backend / target (exit code 3)."""


class UnsupportedError(PracticeError):
    """The script needs something cmmrun does not implement (exit code 5)."""


@dataclass(frozen=True)
class Num:
    v: int
    radix: str = "hex"         # hex | dec | bin | char


@dataclass(frozen=True)
class Flt:
    v: Fraction


@dataclass(frozen=True)
class Bool:
    v: bool


@dataclass(frozen=True)
class Str:
    v: str


@dataclass(frozen=True)
class Addr:
    access: str                # "" when none was given
    v: int


@dataclass(frozen=True)
class Rng:
    lo: Addr
    hi: int                    # inclusive


@dataclass(frozen=True)
class Time:
    s: Fraction                # seconds


Value = Num | Flt | Bool | Str | Addr | Rng | Time


def hex_digits(v: int) -> str:
    return format(v & MASK64, "X")


def hex_plain(v: int) -> str:
    h = hex_digits(v)
    return "0" + h if h[0] in "ABCDEF" else h


def bin_plain(v: int) -> str:
    b = format(v & MASK64, "b")
    return b.rjust((len(b) + 7) // 8 * 8, "0")


def float_text(f: Fraction) -> str:
    if f.denominator == 1:
        return f"{f.numerator}.0"
    neg = f < 0
    f = abs(f)
    # 18 significant digits, trailing zeros removed.
    ip = f.numerator // f.denominator
    int_digits = len(str(ip)) if ip else 0
    frac_digits = max(1, 18 - int_digits)
    scaled = round(f * 10 ** frac_digits)
    s = str(scaled).rjust(frac_digits + 1, "0")
    whole, frac = s[:-frac_digits], s[-frac_digits:].rstrip("0") or "0"
    return ("-" if neg else "") + f"{whole}.{frac}"


def time_text(t: Fraction) -> str:
    return f"{float(t):.10f}s"


def plain(x: Value, fmt: str = "") -> str:
    """How PRINT shows a value. `fmt` is a PRINT format such as %Decimal."""
    if isinstance(x, Num):
        if fmt == "dec":
            return str(x.v)
        if fmt == "hex":
            return hex_plain(x.v)
        if fmt == "bin":
            return bin_plain(x.v)
        if fmt == "ascii" or x.radix == "char":
            return chr(x.v & 0xFF) if 0 <= x.v < 0x110000 else "?"
        if x.radix == "dec":
            return str(x.v)
        if x.radix == "bin":
            return bin_plain(x.v)
        return hex_plain(x.v)
    if isinstance(x, Bool):
        return "TRUE " if x.v else "FALSE"
    if isinstance(x, Str):
        return x.v
    if isinstance(x, Flt):
        return float_text(x.v)
    if isinstance(x, Addr):
        return (x.access + ":" if x.access else "") + hex_plain(x.v)
    if isinstance(x, Rng):
        return plain(x.lo) + "--" + hex_plain(x.hi)
    if isinstance(x, Time):
        return time_text(x.s)
    raise TypeError(x)


def echo(x: Value) -> str:
    """How ECHO shows a value."""
    if isinstance(x, Num):
        if x.radix == "dec":
            return f"{x.v}."
        if x.radix == "bin":
            return "0y" + format(x.v & MASK64, "b")
        if x.radix == "char":
            return chr(x.v & 0xFF)
        return "0x" + hex_digits(x.v)
    if isinstance(x, Bool):
        return "TRUE()" if x.v else "FALSE()"
    if isinstance(x, Addr):
        return (x.access + ":" if x.access else "") + "0x" + hex_digits(x.v)
    if isinstance(x, Rng):
        return echo(x.lo) + "--0x" + hex_digits(x.hi)
    return plain(x)


def macro_text(x: Value) -> str:
    """The text a macro holds after `&m=<expression>`."""
    if isinstance(x, Num) and x.radix in ("hex", "char"):
        h = hex_digits(x.v)
        return "0x" + ("0" + h if len(h) == 1 and h in "ABCDEF" else h)
    return echo(x)


def truth(x: Value) -> bool:
    if isinstance(x, Bool):
        return x.v
    if isinstance(x, Num):
        return x.v != 0
    if isinstance(x, Flt):
        return x.v != 0
    if isinstance(x, Str):
        raise PracticeError(f'a string ("{x.v}") is not a condition')
    raise PracticeError(f"{type(x).__name__} is not a condition")


def to_int(x: Value, what: str = "value") -> int:
    if isinstance(x, Num):
        return x.v
    if isinstance(x, Addr):
        return x.v
    if isinstance(x, Bool):
        return int(x.v)
    if isinstance(x, Flt):
        return int(x.v)
    raise PracticeError(f"{what} must be a number, not {plain(x)!r}")


def to_str(x: Value, what: str = "argument") -> str:
    if isinstance(x, Str):
        return x.v
    raise PracticeError(f"{what} must be a string")
