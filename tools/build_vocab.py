"""Build cmmrun/lang/vocab_data.json from the public TRACE32 PDF manuals.

The manuals must first be converted to UTF-8 text with `pdftotext -layout -enc UTF-8`.
Only names are extracted: command names, function names, and option/keyword words
from "Format:" blocks. This is the same kind of keyword list a syntax highlighter
uses. No manual text is copied.

usage: python tools/build_vocab.py <dir-with-.txt/.u8-files> [-o cmmrun/lang/vocab_data.json]
"""
import argparse
import json
import pathlib
import re

NAME = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*"
RE_FORMAT = re.compile(rf"^\s*Format:\s+({NAME})")
RE_SYNTAX = re.compile(rf"^\s*Syntax:\s+({NAME})\(")
RE_TOC = re.compile(rf"^({NAME})(\(\))?\s{{2,}}[A-Za-z(<'\"].*[^0-9]\s+[0-9]+\s*$")
RE_BULLET = re.compile(rf"\u274f ({NAME})(\(\))?")
RE_WORD = re.compile(r"(?<![<A-Za-z0-9_])/?([A-Za-z][A-Za-z0-9_]*)")

# Names the extraction misses (aliases, or headings laid out unusually).
EXTRA_CMDS = """SYStem.Up SYStem.Down SYStem.Attach SYStem.NoDebug SYStem.StandBy SYStem.Prepare
CD Mode.Hll Mode.Asm Mode.Mix FLASH.TARGET FLASH.TARGET2 FLASH.AUTO FLASH.CFI
Go Step Break SYStem.Mode.Attach SYStem.Mode.Up SYStem.Mode.Go SYStem.Mode.Down
SYStem.Mode.NoDebug SYStem.Mode.Prepare SYStem.Mode.StandBy SYStem.CONFIG.DEBUGPORTTYPE
Data.LOAD.Ihex Data.LOAD.Srecord Data.List FLASH.CLEAR Register.RESet SETUP.LIST SYStem.CpuAccess WinEXT""".split()
EXTRA_FUNCS = """TRUE FALSE Register Data.Long Data.Byte Data.Word Data.Quad Data.Short Data.Float
Data.String CPU FOUND STATE.RUN PRACTICE.ARG""".split()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", default="cmmrun/lang/vocab_data.json")
    a = ap.parse_args()
    cmds, funcs, words = set(EXTRA_CMDS), set(EXTRA_FUNCS), set()
    for p in sorted(pathlib.Path(a.src).iterdir()):
        if p.suffix not in (".u8", ".txt", ".u8x"):
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        fmt_left = 0
        for line in lines:
            if m := RE_FORMAT.match(line):
                cmds.add(m.group(1))
                fmt_left = 12
            elif m := RE_SYNTAX.match(line):
                funcs.add(m.group(1))
            elif m := RE_TOC.match(line):
                (funcs if m.group(2) else cmds).add(m.group(1))
            for m in RE_BULLET.finditer(line):
                (funcs if m.group(2) else cmds).add(m.group(1))
            if fmt_left:
                if not line.strip():
                    fmt_left = 0
                    continue
                fmt_left -= 1
                body = re.sub(r"<[^>]*>", " ", line.split("Format:")[-1])
                for w in RE_WORD.findall(body):
                    if any(c.isupper() for c in w) and len(w) > 1:
                        words.add(w)
    words -= {"Format", "Syntax"}
    out = {"commands": sorted(cmds), "functions": sorted(funcs), "words": sorted(words)}
    pathlib.Path(a.o).write_text(json.dumps(out, indent=0) + "\n", encoding="utf-8")
    print({k: len(v) for k, v in out.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
