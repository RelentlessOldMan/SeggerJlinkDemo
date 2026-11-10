"""Run PRACTICE scripts in the TRACE32 instruction-set simulator, headless, and capture their output.

Used to record reference ("golden") output for cmmrun's language tests. Needs a TRACE32
simulator installation; the free Arm simulator from lauterbach.com is enough for
language-only scripts (no target execution).

    python tools/t32sim.py --t32 <dir with t32marm.exe> script.cmm [args...]
    python tools/t32sim.py --t32 <dir> --record tests/cmm      # rewrite every *.expected

Output of PRINT goes to an AREA that is logged to a file. Errors that stop the
script show up there too, as TRACE32 reports them.

Note: do not redirect the simulator's stdout to NUL; it then does not run the script.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

WRAPPER = """\
AREA.Create CMMRUNLOG 200. 10000.
AREA.OPEN CMMRUNLOG "{log}"
AREA.Select CMMRUNLOG
ON ERROR GOTO cmmrun_failed
DO "{script}" {args}
AREA.CLOSE CMMRUNLOG
QUIT
cmmrun_failed:
PRINT "<<error " ERROR.ID() ": " ERROR.MESSAGE() " | " ERROR.CMDLINE() ">>"
AREA.CLOSE CMMRUNLOG
QUIT
"""


def find_t32(hint: str | None) -> pathlib.Path:
    for d in [hint, os.environ.get("T32SYS"), "_downloads/trace32-simarm/sim", "C:/T32"]:
        if d and (pathlib.Path(d) / "t32marm.exe").exists():
            return pathlib.Path(d).resolve()
    sys.exit("t32sim: TRACE32 simulator (t32marm.exe) not found; pass --t32 <dir>")


def run(script: pathlib.Path, args: list[str], t32: pathlib.Path, timeout: float = 60) -> list[str]:
    script = script.resolve()
    with tempfile.TemporaryDirectory(prefix="t32sim-") as tmp:
        tmp_p = pathlib.Path(tmp)
        log = tmp_p / "out.txt"
        (tmp_p / "cfg.t32").write_text(
            f"OS=\nID=T32cmmrunsim\nSYS={t32}\nTMP={tmp_p}\n\nPBI=SIM\n\nSCREEN=OFF\n", encoding="ascii")
        (tmp_p / "wrap.cmm").write_text(
            WRAPPER.format(log=log, script=script, args=" ".join(args)), encoding="ascii")
        subprocess.run([str(t32 / "t32marm.exe"), "-c", str(tmp_p / "cfg.t32"), "-s", str(tmp_p / "wrap.cmm")],
                       cwd=str(tmp_p), timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not log.exists():
            raise RuntimeError(f"no output log for {script} (did the simulator start?)")
        text = log.read_bytes().decode("cp1252", errors="replace")
    lines = text.replace("\r\n", "\n").split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t32")
    ap.add_argument("--record", metavar="DIR", help="run every DIR/*.cmm and write DIR/<name>.expected")
    ap.add_argument("script", nargs="?")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    t32 = find_t32(a.t32)
    if a.record:
        for p in sorted(pathlib.Path(a.record).glob("*.cmm")):
            if p.name.startswith("_"):
                continue        # helper scripts called by the tests
            lines = run(p, [], t32)
            p.with_suffix(".expected").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            print(f"{p.name}: {len(lines)} lines")
        return 0
    if not a.script:
        ap.error("script required")
    for ln in run(pathlib.Path(a.script), a.args, t32):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
