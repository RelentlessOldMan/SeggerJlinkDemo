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
    p.add_argument("--exclude", nargs="+", default=[], metavar="GLOB",
                   help="skip directories or files whose relative path or name matches (e.g. archive '*/old/*')")
    p.add_argument("--jobs", type=int, default=16,
                   help="parallel directory listings and file reads (default 16; raise for slow network shares)")
    p.add_argument("--quiet", action="store_true", help="no progress lines on stderr")


def _write(out_dir: str, stem: str, data: dict, md: str) -> tuple[pathlib.Path, pathlib.Path]:
    d = pathlib.Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp, mp = d / f"{stem}.json", d / f"{stem}.md"
    jp.write_text(json.dumps(data, indent=1, ensure_ascii=True), encoding="utf-8")
    mp.write_text(md, encoding="utf-8")
    return jp, mp


class UsageError(Exception):
    pass


def _prepare(a, stem: str):
    """Check every path and input before a (possibly slow) load of the tree."""
    from .analysis.anonymize import SaltError, load_salt
    root = pathlib.Path(a.root)
    if not root.exists():
        raise UsageError(f"{a.root} does not exist")
    if not root.is_dir():
        raise UsageError(f"{a.root} is not a directory")
    out = pathlib.Path(a.out)
    if out.exists() and not out.is_dir():
        raise UsageError(f"--out {a.out} is a file, not a directory")
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise UsageError(f"cannot create --out {a.out}: {e.strerror or e}") from None
    if getattr(a, "private_map", None):
        pm = pathlib.Path(a.private_map)
        if "PRIVATE" not in pm.name.upper():
            pm = pm.with_name(f"{pm.stem}-PRIVATE{pm.suffix or '.json'}")
        if pm.is_dir():
            raise UsageError(f"--private-map {pm} is a directory")
        if pm.resolve().parent == out.resolve():
            raise UsageError("--private-map must not be in the --out directory, next to the reports "
                             "you are going to share")
        a.private_map = str(pm)
    if getattr(a, "hints", None):
        from .analysis.check import load_hints
        try:
            a.hints = load_hints(a.hints)
        except ValueError as e:
            raise UsageError(str(e)) from None
    if not a.private:
        try:
            a.salt = load_salt(a.salt_file)
        except SaltError as e:
            raise UsageError(str(e)) from None
    else:
        a.salt = b"\0" * 16


def _anon(a, vocab):
    from .analysis.anonymize import Anonymizer
    return Anonymizer(vocab, a.salt, enabled=not a.private)


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


def _mib(n: int) -> str:
    return f"{n / 2**20:,.1f} MiB"


def cmd_inventory(a) -> int:
    from .corpus import inventory
    t0 = time.time()
    if not pathlib.Path(a.root).is_dir():
        print(f"cmmrun: {a.root} is not an existing directory", file=sys.stderr)
        return EXIT_SCRIPT
    r = inventory(a.root, tuple(a.ext), excludes=tuple(a.exclude), jobs=a.jobs, progress=not a.quiet, top=a.top)
    if not r["files"]:
        print(f"no files with extensions {a.ext} under {a.root}", file=sys.stderr)
        return EXIT_SCRIPT
    dup = 1 - r["distinct_bytes"] / r["bytes"] if r["bytes"] else 0
    sk = ", ".join(f"{k} {n}" for k, n in r["skipped"].items() if n) or "none"
    e = r["estimate"]
    print(f"scripts:          {r['files']:,} files, {_mib(r['bytes'])}")
    print(f"distinct content: {r['distinct_files']:,} files, {_mib(r['distinct_bytes'])} ({dup:.0%} of the bytes are copies)")
    print(f"skipped:          {sk}")
    print(f"full scan/check:  about {e['memory_mib']:,} MiB of memory and {e['minutes']} min each (rough estimate)")
    print(f"inventory took {time.time() - t0:.1f} s")
    print("\nlargest top-level directories by distinct content (real names: local only, do not share):")
    w = max([len(t["dir"]) for t in r["top_dirs"]] + [3])
    for t in r["top_dirs"]:
        print(f"  {t['dir']:<{w}}  {t['files']:>8,} files  {_mib(t['bytes']):>12}  distinct {_mib(t['distinct_bytes']):>12}")
    print("\nuse --exclude to leave out archives or copies, e.g. --exclude old '*/archive/*'")
    return EXIT_OK


def cmd_scan(a) -> int:
    from .analysis.report import scan_markdown
    from .analysis.scan import scan
    from .corpus import load_corpus
    t0 = time.time()
    try:
        _prepare(a, "cmmrun-scan")
    except UsageError as e:
        print(f"cmmrun: {e}", file=sys.stderr)
        return EXIT_SCRIPT
    c = load_corpus(a.root, tuple(a.ext), excludes=tuple(a.exclude), jobs=a.jobs, progress=not a.quiet)
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
    try:
        _prepare(a, "cmmrun-check")
    except UsageError as e:
        print(f"cmmrun: {e}", file=sys.stderr)
        return EXIT_SCRIPT
    c = load_corpus(a.root, tuple(a.ext), excludes=tuple(a.exclude), jobs=a.jobs, progress=not a.quiet)
    if not c.files:
        print(f"no files with extensions {a.ext} under {a.root}", file=sys.stderr)
        return EXIT_SCRIPT
    anon = _anon(a, c.vocab)
    data = check(c, anon, backend=a.backend, hints=a.hints)
    _finish(a, anon, c.vocab, data, "cmmrun-check", check_markdown)
    v = data["verdicts"]["entry_only"]
    print("entry scripts: " + ", ".join(f"{k} {n}" for k, n in v.items()))
    return EXIT_OK


def cmd_probes(a) -> int:
    """List SEGGER probes from the OS USB device list. Opens nothing."""
    from .backends.jlink import list_probes, load_config, user_deny_serials
    from .interp.values import TargetError
    try:
        deny = load_config(a.target).deny_serials if a.target else user_deny_serials()
        probes = list_probes()
    except TargetError as e:
        print(f"cmmrun: {e}", file=sys.stderr)
        return 3
    if not probes:
        print("no SEGGER probes found")
    for p in probes:
        notes = [n for n, on in (("DENIED", p.serial in deny), ("on-board? (refused unless allow_onboard)",
                                                               p.composite)) if on]
        print(f"{p.serial:>12}  PID {p.pid:04X}  {p.name:<24} {'  '.join(notes)}")
    return EXIT_OK


def cmd_run(a) -> int:
    from .interp.runner import run_cli
    return run_cli(a)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cmmrun", description="Analyse and run PRACTICE (.cmm) scripts.")
    ap.add_argument("--version", action="version", version=f"cmmrun {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probes", help="list SEGGER probes from the USB device list (opens none)")
    p.add_argument("--target", help="target.yaml, to mark its denied serials")
    p.set_defaults(func=cmd_probes)

    p = sub.add_parser("inventory", help="quick look at a big tree: sizes, copies, and what a full scan costs (no parsing)")
    _common_analysis_args(p)
    p.add_argument("--top", type=int, default=15, help="top-level directories to list")
    p.set_defaults(func=cmd_inventory)

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
    p.add_argument("--dry-run", action="store_true", help="run on the mock target instead of hardware, to check the script first")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--log", help="write a JSON log of target changes here")
    p.set_defaults(func=cmd_run)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
