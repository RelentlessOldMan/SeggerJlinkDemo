"""Run tests/cmm/*.cmm through cmmrun (mock target) and compare with output recorded from TRACE32.

The .expected files come from the TRACE32 instruction-set simulator:
    python tools/t32sim.py --record tests/cmm
"""
import pathlib

import pytest

from cmmrun.backends.mock import MockBackend
from cmmrun.interp.interpreter import Interpreter

CMM = pathlib.Path(__file__).parent / "cmm"
# Scripts that need target execution: the free Arm simulator cannot run code, so
# there is no TRACE32 reference for them. They are validated on the mock backend
# instead (see test_run_control in test_interp.py). Keep this in sync with t32sim.py.
NON_GOLDEN = {"run"}
CASES = sorted(p for p in CMM.glob("*.cmm")
               if not p.name.startswith("_") and p.stem not in NON_GOLDEN)


def run_script(path: pathlib.Path) -> list[str]:
    it = Interpreter(MockBackend(), cwd=str(path.parent), timeout=30)
    it.run(path)
    return it.out_lines


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_matches_trace32(path):
    expected = path.with_suffix(".expected").read_text(encoding="utf-8").splitlines()
    got = run_script(path)
    diffs = [f"line {i + 1}: expected {e!r}, got {g!r}"
             for i, (e, g) in enumerate(zip(expected, got)) if e != g]
    if len(got) != len(expected):
        diffs.append(f"{len(got)} lines, expected {len(expected)}")
    assert not diffs, "\n".join(diffs)
