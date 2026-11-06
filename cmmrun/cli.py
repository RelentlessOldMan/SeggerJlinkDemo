"""cmmrun command line: scan | check | run."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from . import __version__

EXIT_OK, EXIT_SCRIPT, EXIT_TARGET, EXIT_TIMEOUT, EXIT_UNSUPPORTED = 0, 2, 3, 4, 5


def _common_analysis_args(p):
    p.add_argument("root", help="directory to search for scripts")
    p.add_argument("--out", default=".", help="directory for the report files (default: current)")
    p.add_argument("--ext", nargs="+", default=[".cmm"], help="file extensions to include (default: .cmm)")
    p.add_argument("--private", action="store_true",
                   help="do NOT anonymize (real names and paths). For local use only, never share")
    p.add_argument("--private-map", metavar="FILE",
                   help="also write a local id->real-name map (never share this file)")
    p.add_argument("--salt-file", help="salt file for ids (default ~/.cmmrun/salt)")


def _write(out_dir: str, stem: str, data: dict, md: str) -> tuple[pathlib.Path, pathlib.Path]:
    d = pathlib.Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp, mp = d / f"{stem}.json", d / f"{stem}.md"
    jp.write_text(json.dumps(data, indent=1, ensure_ascii=True), encoding="utf-8")
    mp.write_text(md, encoding="utf-8")
    return jp, mp


def _anon(a, vocab):
    from .analysis.anonymize import Anonymizer, load_salt
    return Anonymizer(vocab, load_salt(a.salt_file), enabled=not a.private)


def _finish(a, anon, vocab, data, stem, render):
    from .analysis.anonymize import guard
    from .analysis.report import ALLOWED_TERMS
    if anon.enabled:
        data, n = guard(data, vocab, ALLOWED_TERMS)
        data["leak_guard_redactions"] = n
    md = render(data)
    jp, mp = _write(a.out, stem + ("-PRIVATE" if not anon.enabled else ""), data, md)
    if a.private_map:
        pathlib.Path(a.private_map).write_text(json.dumps(anon.private_map, indent=1), encoding="utf-8")
        print(f"private map (do NOT share): {a.private_map}")
    print(f"wrote {mp}\nwrote {jp}")
    if not anon.enabled:
        print("NOTE: --private report contains real names. Do not share it.")
    elif data.get("leak_guard_redactions"):
        print(f"WARNING: leak guard redacted {data['leak_guard_redactions']} words; please report this as a bug")


def cmd_scan(a) -> int:
    from .analysis.report import scan_markdown
    from .analysis.scan import scan
    from .corpus import load_corpus
    t0 = time.time()
    c = load_corpus(a.root, tuple(a.ext))
    if not c.files:
        print(f"no files with extensions {a.ext} under {a.root}", file=sys.stderr)
        return EXIT_SCRIPT
    anon = _anon(a, c.vocab)
    data = scan(c, anon)
    data["elapsed_s"] = round(time.time() - t0, 2)
    _finish(a, anon, c.vocab, data, "cmmrun-scan", lambda d: scan_markdown(d, a.top))
    print(f"{len(c.files)} files scanned in {data['elapsed_s']} s")
    return EXIT_OK


def cmd_check(a) -> int:
    from .analysis.check import check
    from .analysis.report import check_markdown
    from .corpus import load_corpus
    c = load_corpus(a.root, tuple(a.ext))
    if not c.files:
        print(f"no files with extensions {a.ext} under {a.root}", file=sys.stderr)
        return EXIT_SCRIPT
    anon = _anon(a, c.vocab)
    data = check(c, anon, backend=a.backend, hints=a.hints)
    _finish(a, anon, c.vocab, data, "cmmrun-check", check_markdown)
    v = data["verdicts"]["entry_only"]
    print("entry scripts: " + ", ".join(f"{k} {n}" for k, n in v.items()))
    return EXIT_OK


def cmd_run(a) -> int:
    from .interp.runner import run_cli
    return run_cli(a)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cmmrun", description="Analyse and run PRACTICE (.cmm) scripts.")
    ap.add_argument("--version", action="version", version=f"cmmrun {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="statistics about a directory of scripts (anonymized, shareable)")
    _common_analysis_args(p)
    p.add_argument("--top", type=int, default=60, help="rows in the command table")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("check", help="which entry scripts could run on a backend, and what blocks the rest")
    _common_analysis_args(p)
    p.add_argument("--backend", default="jlink", choices=["jlink", "mock"], help="support table to check against")
    p.add_argument("--hints", help="hints.yaml with known macro values")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("run", help="run a script")
    p.add_argument("script")
    p.add_argument("args", nargs="*", help="arguments passed to the script (ENTRY)")
    p.add_argument("--backend", default="mock", choices=["mock", "jlink"])
    p.add_argument("--target", help="target.yaml (required for jlink)")
    p.add_argument("--allow-dynamic", action="store_true", help="allow lines whose command is only known at run time")
    p.add_argument("--dry-run", action="store_true", help="run against the mock backend first")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--log", help="write a JSON log of target changes here")
    p.set_defaults(func=cmd_run)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
