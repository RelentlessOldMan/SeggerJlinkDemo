"""Anonymization for shareable reports.

Rules:
- Public PRACTICE/TRACE32 vocabulary (command, function, option and keyword
  names from the public manuals) is shown as is.
- Any other name becomes a stable salted id such as `u3fa9c1`. The salt is
  random per machine (stored in ~/.cmmrun/salt), so ids are stable between
  runs on one machine but mean nothing elsewhere.
- File paths become F0001..., numbered in salted-hash order (not alphabetical).
- Arguments, numbers, strings, comments, labels and macro names are never
  output; code shapes use placeholders (<num>, <str>, &m1, ...).
- A final allowlist guard checks every word in the report data and redacts
  anything that is not public vocabulary, a report term, or an id.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import pathlib
import re
import secrets

from ..lang import lexer
from ..lang.parser import Stmt, functions_in
from ..lang.vocab import Vocab

ID_RE = re.compile(r"^(u[0-9a-f]{6}|F\d{4,}|m\d+)$")
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_salt(path: str | None = None) -> bytes:
    p = pathlib.Path(path) if path else pathlib.Path.home() / ".cmmrun" / "salt"
    try:
        return bytes.fromhex(p.read_text().strip())
    except (OSError, ValueError):
        salt = secrets.token_bytes(16)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(salt.hex())
        except OSError:
            pass
        return salt


class Anonymizer:
    def __init__(self, vocab: Vocab, salt: bytes, enabled: bool = True):
        self.v = vocab
        self.salt = salt
        self.enabled = enabled
        self.private_map: dict[str, str] = {}

    # -- ids
    def hid(self, token: str, kind: str = "u") -> str:
        h = hmac.new(self.salt, f"{kind}:{token.upper()}".encode(), hashlib.sha256).hexdigest()[:6]
        ident = "u" + h
        self.private_map.setdefault(ident, token)
        return ident

    def file_ids(self, rels) -> dict[str, str]:
        if not self.enabled:
            return {r: r for r in rels}
        order = sorted(rels, key=lambda r: hmac.new(self.salt, r.lower().encode(), hashlib.sha256).digest())
        ids = {r: f"F{i + 1:04d}" for i, r in enumerate(order)}
        for r, i in ids.items():
            self.private_map[i] = r
        return ids

    # -- names
    def word(self, w: str) -> str:
        if not self.enabled or self.v.is_word(w):
            return w
        return self.hid(w)

    def command(self, st: Stmt) -> str:
        """Display name for a statement's command."""
        typed = st.cmd
        m = st.match
        if not self.enabled:
            return m.canon if m and m.status == "known" else typed
        if m and m.status == "known":
            return m.canon
        segs = typed.split(".")
        known = m.canon.split(".") if m and m.canon else []
        rest = segs[len(known):]
        shown = []
        for s in rest:
            if "&" in s:
                shown.append("<macro>")
            elif self.v.is_word(s):
                shown.append(s.upper() + "~")
            else:
                shown.append(self.hid(".".join(known + [s])))
        return ".".join(known + shown)

    def function(self, typed: str) -> str:
        m = self.v.function(typed)
        if m.status == "known" or not self.enabled:
            return m.canon if m.status == "known" else typed
        known = m.canon.split(".") if m.canon else []
        rest = typed.split(".")[len(known):]
        shown = [s.upper() + "~" if self.v.is_word(s) else self.hid(typed) for s in rest]
        return ".".join(known + shown)

    def option(self, opt: str) -> str:
        body = opt[1:]
        if "&" in body:
            return "/<macro>"
        return "/" + self.word(body)

    # -- code shapes
    def shape(self, text: str, macro_ids: dict[str, str] | None = None, limit: int = 120) -> str:
        """Replace every literal and name in an expression or argument list with placeholders."""
        if not self.enabled:
            return text[:limit]
        macro_ids = {} if macro_ids is None else macro_ids
        out: list[str] = []
        i, n = 0, len(text)
        funcs = set(functions_in(text))
        while i < n:
            c = text[i]
            if c == '"':
                j = i + 1
                while j < n and not (text[j] == '"' and (j + 1 >= n or text[j + 1] != '"')):
                    j += 2 if text[j] == '"' else 1
                body = text[i + 1:j]
                ms = lexer.macros_in(body)
                out.append("<str" + "".join(f"+&{self._mid(m, macro_ids)}" for m in ms) + ">")
                i = j + 1
                continue
            if c == "&":
                m = lexer.MACRO_RE.match(text, i)
                if m:
                    out.append("&" + self._mid(m.group(1) or m.group(2), macro_ids))
                    i = m.end()
                    continue
            m = re.match(r"[A-Za-z]{1,4}:(?=[0-9&])", text[i:])
            if m:
                out.append("<ac>:")
                i += m.end()
                continue
            m = re.match(r"0x[0-9A-Fa-f_]+|0y[01_]+|[0-9][0-9_]*\.?[0-9]*(?:ms|us|ns|s)?", text[i:])
            if m:
                out.append("<num>")
                i += m.end()
                continue
            m = re.match(r"[A-Za-z_~][A-Za-z0-9_.~/\\\-]*", text[i:])
            if m:
                tok = m.group(0)
                if "/" in tok or "\\" in tok or tok.startswith("~"):
                    out.append("<path>")
                elif tok in funcs:
                    out.append(self.function(tok))
                elif self.v.is_word(tok):
                    out.append(tok)
                else:
                    out.append("<id>")
                i += m.end()
                continue
            if c in " \t":
                if out and out[-1] != " ":
                    out.append(" ")
                i += 1
                continue
            out.append(c)
            i += 1
        s = "".join(out).strip()
        s = re.sub(r"(<num>)(\s*<num>)+", r"\1", s)
        return s if len(s) <= limit else s[:limit] + "..."

    def stmt_shape(self, st: Stmt, macro_ids: dict[str, str] | None = None) -> str:
        macro_ids = {} if macro_ids is None else macro_ids
        if st.kind == "assign":
            return f"&{self._mid(st.name, macro_ids)}={self.shape(st.rhs, macro_ids)}"
        if st.kind != "command":
            return st.kind
        parts = [self.command(st) if "&" not in st.cmd else self._cmd_with_macro(st, macro_ids)]
        for a in st.args:
            if lexer.is_option(a):
                parts.append(self.option(a))
            elif a.startswith("%"):
                parts.append("%" + self.word(a[1:]) if re.fullmatch(r"%[A-Za-z]+", a) else "%<fmt>")
            else:
                parts.append(self.shape(a, macro_ids))
        s = " ".join(parts)
        return s if len(s) <= 160 else s[:160] + "..."

    def _cmd_with_macro(self, st: Stmt, macro_ids) -> str:
        lit = st.cmd.split("&", 1)[0].rstrip(".")
        names = lexer.macros_in(st.cmd)
        prefix = (self.command(Stmt("command", 0, cmd=lit, match=self.v.command(lit))) + ".") if lit else ""
        return prefix + "&" + self._mid(names[0], macro_ids) if names else "<macro>"

    @staticmethod
    def _mid(name: str, macro_ids: dict[str, str]) -> str:
        if name not in macro_ids:
            macro_ids[name] = f"m{len(macro_ids) + 1}"
        return macro_ids[name]


# -------------------------------------------------------------- leak guard

def guard(data, vocab: Vocab, allowed_terms: set[str]) -> tuple[object, int]:
    """Return (cleaned data, number of redactions).

    Every word in every string (keys and values) must be public vocabulary, an
    allowed report term, or an id. Anything else is replaced with [redacted].
    """
    count = 0
    allowed_upper = {t.upper() for t in allowed_terms}

    def cheap_ok(w: str) -> bool:
        return bool(ID_RE.match(w)) or w.upper() in allowed_upper or vocab.is_word(w)

    def known_name(w: str) -> bool:
        return vocab.command(w).status == "known" or vocab.function(w).status == "known"

    def ok_word(w: str) -> bool:
        return cheap_ok(w) or known_name(w)

    def clean_str(s: str) -> str:
        nonlocal count

        def rep(m):
            nonlocal count
            w = m.group(0)
            if ok_word(w):
                return w
            count += 1
            return "[redacted]"

        # check dotted names as a whole first
        def rep_dotted(m):
            name = m.group(0)
            if "." not in name:
                return name if ok_word(name) else rep(m)
            if known_name(name):
                return name
            return WORD_RE.sub(rep, name)

        return re.sub(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", rep_dotted, s)

    def walk(x):
        if isinstance(x, dict):
            return {clean_str(str(k)): walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            return clean_str(x)
        return x

    return walk(data), count


def private_map_path_ok(path: str) -> bool:
    return bool(path) and not os.path.isdir(path)
