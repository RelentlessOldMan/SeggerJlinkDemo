"""What cmmrun supports, in canonical PRACTICE names.

Derived from the interpreter's own command and function tables, so `check`
and the pre-flight of `run` always agree: a command is "supported" exactly
when the interpreter has code for it, "ignored" when it is accepted and
skipped (window and GUI layout commands that do not change the target), and
"unsupported" otherwise.
"""
from __future__ import annotations

from ..interp import functions
from ..lang import lexer
from ..interp.interpreter import COMMANDS, IGNORED, TARGET_COMMANDS

# Handled by the interpreter's control flow rather than a command method.
CONTROL = {"IF", "ELSE", "WHILE", "RePeaT", "LOCAL", "PRIVATE", "GLOBAL", "ENTRY", "PARAMETERS", "RETURNVALUES"}


class Support:
    def __init__(self, backend: str = "jlink"):
        # The mock and J-Link backends implement the same command set.
        self.backend = backend
        self.commands = set(COMMANDS) | CONTROL
        self.ignored = set(IGNORED)
        self.target_commands = set(TARGET_COMMANDS)
        self.functions = set(functions.REGISTRY)

    def command(self, canon: str) -> str:
        if canon in self.commands:
            return "supported"
        if canon in self.ignored:
            return "ignored"
        return "unsupported"

    def function(self, canon: str) -> bool:
        return canon in self.functions

    @staticmethod
    def unsupported_event(st) -> str | None:
        """`ON <event>` is supported only for ERROR; the name of the unsupported form otherwise."""
        if st.kind != "command" or st.canon != "ON":
            return None
        ev = on_event(st)
        return None if ev == "ERROR" else f"ON {ev or '?'}"


def on_event(st) -> str:
    pos = [a for a in st.args if not lexer.is_option(a)]
    return pos[0].upper() if pos else ""


def user_commands(stmts) -> set[str]:
    """Names declared as user-defined commands with ON CMD or GLOBALON CMD (upper case)."""
    out = set()
    for st in stmts:
        if st.kind == "command" and st.canon in ("ON", "GLOBALON"):
            pos = [a for a in st.args if not lexer.is_option(a)]
            if len(pos) > 1 and pos[0].upper() == "CMD":
                out.add(pos[1].upper())
    return out
