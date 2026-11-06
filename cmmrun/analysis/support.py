"""What each backend supports, in terms of canonical PRACTICE names.

This is the *planned* support of cmmrun's interpreter and backends. `check`
uses it to predict which scripts could run. Keep it honest: list a command
here only when the interpreter implements it (or will in the current
milestone, marked in the README).

Each entry is "supported" (executed), or "ignored" (accepted and skipped,
e.g. window and GUI layout commands that have no effect on the target).
Anything else is unsupported.
"""
from __future__ import annotations

# Pure language: the interpreter handles these on every backend.
LANGUAGE = """
PRINT ECHO ENDDO END DO RUN GOSUB RETURN GOTO JUMPTO IF ELSE WHILE RePeaT
LOCAL PRIVATE GLOBAL ENTRY PARAMETERS RETURNVALUES SUBROUTINE WAIT STOP CONTinue
ON GLOBALON OPEN CLOSE READ WRITE APPEND CD ChDir ChDir.DO
""".split()

# Window, GUI and host-only commands that do not change the target: accepted and skipped.
IGNORED = """
WinCLEAR WinPOS WinPAGE WinRESIZE WinTOP WinEXT WinOverlay WinFreeze WinPrint
AREA AREA.view AREA.Create AREA.Select AREA.CLEAR AREA.Delete AREA.CLOSE AREA.OPEN AREA.RESet
SCREEN SCREEN.ON SCREEN.OFF SCREEN.display SCREEN.WAIT SCREEN.ALways
Mode.Hll Mode.Asm Mode.Mix TOOLBAR STATUSBAR MENU.ReProgram MENU.RESet BEEP
List List.auto Register.view Data.List Data.dump Data.View PER.view SETUP.Var SETUP.LIST
TITLE FramePOS SYStem.Option.ResBreak SYStem.Option.EnReset SYStem.Option.IMASKASM
SYStem.Option.IMASKHLL SYStem.MemAccess SYStem.CpuAccess SYStem.CONFIG.DEBUGPORTTYPE
""".split()

# Target commands the J-Link backend implements (or will in milestone 1).
JLINK = """
SYStem.Up SYStem.Down SYStem.Attach SYStem.Mode.Up SYStem.Mode.Down SYStem.Mode.Attach
SYStem.Mode.NoDebug SYStem.Mode.Go SYStem.CPU SYStem.JtagClock SYStem.RESet RESet
Data.Set Data.LOAD.Elf Data.LOAD.Binary Data.LOAD.Ihex Data.LOAD.Srecord Data.SAVE.Binary
Register.Set Register.RESet Go Break Step
FLASH.RESet FLASH.Create FLASH.TARGET FLASH.ReProgram FLASH.Erase FLASH.Program FLASH.AUTO
""".split()

# Functions (by canonical name or prefix ending in ".").
FUNCTIONS_LANG = """
TRUE FALSE FOUND OS.PresentPracticeDirectory OS.PresentWorkingDirectory OS.FILE OS.FILE.
STRing. FORMAT. CONVert. Var.STRing PRACTICE.ARG.
""".split()
FUNCTIONS_JLINK = """
Data.Long Data.Byte Data.Word Data.Quad Data.Short Register STATE.RUN
""".split()


class Support:
    def __init__(self, backend: str = "jlink"):
        self.backend = backend
        self.commands = set(LANGUAGE) | set(JLINK)   # the mock backend models the same commands
        self.ignored = set(IGNORED)
        self.functions = FUNCTIONS_LANG + FUNCTIONS_JLINK

    def command(self, canon: str) -> str:
        if canon in self.commands:
            return "supported"
        if canon in self.ignored:
            return "ignored"
        return "unsupported"

    def function(self, canon: str) -> bool:
        return any(canon == f or (f.endswith(".") and canon.startswith(f)) for f in self.functions)
