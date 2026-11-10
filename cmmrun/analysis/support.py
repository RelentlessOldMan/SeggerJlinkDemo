"""What cmmrun supports, in canonical PRACTICE names.

Derived from the interpreter's own command and function tables, so `check`
and the pre-flight of `run` always agree: a command is "supported" exactly
when the interpreter has code for it, "ignored" when it is accepted and
skipped (window and GUI layout commands that do not change the target), and
"unsupported" otherwise.
"""
from __future__ import annotations

from ..interp import functions
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
