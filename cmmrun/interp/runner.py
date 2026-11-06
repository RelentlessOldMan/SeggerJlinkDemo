"""`cmmrun run`: not implemented yet (milestone 2: interpreter + mock and J-Link backends)."""
from __future__ import annotations

import sys


def run_cli(a) -> int:
    print("cmmrun run: not implemented yet. Use `cmmrun check` to see what would run.", file=sys.stderr)
    return 5
