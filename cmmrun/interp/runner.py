"""`cmmrun run`: pre-flight check, then run a script on a backend.

Pre-flight loads every script the entry script can reach through literal
DO/RUN paths and refuses to start if any of them uses a command or function
cmmrun does not implement, jumps to a missing label, or (without
--allow-dynamic) builds a command, script path or jump target from macros.
That way a script either runs from start to end or does not touch the target.

Exit codes: 0 ok, 2 script error, 3 target error, 4 timeout, 5 unsupported.
"""
from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass, field

from ..lang.parser import call_target, jump_target, walk
from . import functions
from .interpreter import Frame, Interpreter, ScriptFailure, Timeout
from .values import PracticeError, TargetError, UnsupportedError

EXIT_OK, EXIT_SCRIPT, EXIT_TARGET, EXIT_TIMEOUT, EXIT_UNSUPPORTED = 0, 2, 3, 4, 5
BLOCKING_DYNAMIC = {"command", "subcommand", "script_path", "jump_target", "option"}


@dataclass
class Problem:
    where: str
    kind: str        # unsupported_command | unsupported_function | missing_label | missing_script | dynamic
    detail: str

    def __str__(self):
        return f"{self.where}: {self.kind.replace('_', ' ')}: {self.detail}"


@dataclass
class Preflight:
    scripts: list[pathlib.Path] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    dynamic: list[Problem] = field(default_factory=list)


def function_supported(vocab, name: str) -> bool:
    m = vocab.function(name)
    return (m.canon if m.status != "unknown" else name) in functions.REGISTRY or name in functions.REGISTRY


def command_supported(canon: str) -> bool:
    from ..analysis.support import Support
    return Support().command(canon) != "unsupported"


def preflight(it: Interpreter, entry: pathlib.Path) -> Preflight:
    pf = Preflight()
    todo, seen = [entry], set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        pf.scripts.append(path)
        try:
            prog = it.program(path)
        except PracticeError as e:
            pf.problems.append(Problem(path.name, "parse_error", str(e)))
            continue
        for st0 in prog.ins:
            for st in walk([st0]):
                where = f"{path.name}:{st.lineno}"
                for name in st.functions:
                    if "&" not in name and not function_supported(it.vocab, name):
                        pf.problems.append(Problem(where, "unsupported_function", name + "()"))
                if st.kind != "command":
                    continue
                dyn = [d for d in st.dynamic if d in BLOCKING_DYNAMIC]
                if dyn:
                    pf.dynamic.append(Problem(where, "dynamic", f"{', '.join(dyn)} in {st.text!r}"))
                    continue
                canon = st.canon
                if not canon or not command_supported(canon):
                    pf.problems.append(Problem(where, "unsupported_command", canon or st.cmd))
                    continue
                if canon in ("GOTO", "GOSUB", "JUMPTO") or (canon == "ON" and jump_target(st)):
                    tgt = jump_target(st)
                    if tgt and "&" not in tgt and tgt not in prog.labels and tgt not in prog.subs:
                        pf.problems.append(Problem(where, "missing_label", tgt))
                if canon in ("DO", "RUN", "ChDir.DO") or (canon == "ON" and call_target(st)):
                    tgt = call_target(st)
                    if tgt and "&" not in tgt:
                        saved = it.frame
                        try:
                            it.frame = Frame("script", None, prog)
                            todo.append(it.resolve_script(tgt, it.frame))
                        except UnsupportedError as e:
                            pf.problems.append(Problem(where, "unsupported_path", str(e)))
                        except PracticeError:
                            pf.problems.append(Problem(where, "missing_script", tgt))
                        finally:
                            it.frame = saved
    return pf


def make_backend(a):
    if a.backend == "mock" or a.dry_run:
        from ..backends.mock import MockBackend
        return MockBackend()
    from ..backends import jlink   # noqa: F401  (milestone: J-Link backend)
    raise UnsupportedError("the J-Link backend is not available yet; use --backend mock")


def run_cli(a) -> int:
    def out(text, newline):
        sys.stdout.write(("\n" if newline and out.started else "") + text)
        sys.stdout.flush()
        out.started = True
    out.started = False

    try:
        backend = make_backend(a)
    except (UnsupportedError, ImportError) as e:
        print(f"cmmrun: {e}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    it = Interpreter(backend, out=out, timeout=a.timeout)
    try:
        entry = it.resolve_script(a.script, None)
    except PracticeError as e:
        print(f"cmmrun: {e}", file=sys.stderr)
        return EXIT_SCRIPT

    pf = preflight(it, entry)
    blockers = pf.problems + ([] if a.allow_dynamic else pf.dynamic)
    if blockers:
        print(f"cmmrun: pre-flight failed for {entry.name} ({len(pf.scripts)} script(s) checked):", file=sys.stderr)
        for p in blockers[:50]:
            print(f"  {p}", file=sys.stderr)
        if len(blockers) > 50:
            print(f"  ... and {len(blockers) - 50} more", file=sys.stderr)
        if pf.dynamic and not a.allow_dynamic:
            print("  (lines built from macros are refused unless --allow-dynamic is given)", file=sys.stderr)
        return EXIT_UNSUPPORTED

    code = EXIT_OK
    try:
        it.run(entry, a.args)
    except ScriptFailure as f:
        e = f.exc
        code = (EXIT_UNSUPPORTED if isinstance(e, UnsupportedError) else EXIT_TARGET if isinstance(e, TargetError)
                else EXIT_TIMEOUT if isinstance(e, Timeout) else EXIT_SCRIPT)
        if out.started:
            sys.stdout.write("\n")
            out.started = False
        print(f"cmmrun: {f}", file=sys.stderr)
    finally:
        backend.close()
    if out.started:
        sys.stdout.write("\n")
    sys.stdout.flush()
    for w in it.warnings:
        print(f"cmmrun: warning: {w}", file=sys.stderr)
    if a.log:
        events = [{"t": e.t, "op": e.op, **e.detail} for e in backend.events]
        pathlib.Path(a.log).write_text(json.dumps({"backend": backend.name, "exit": code, "events": events},
                                                  indent=1), encoding="utf-8")
    if a.dry_run:
        print(f"cmmrun: dry run on the mock target: exit {code}, {len(backend.events)} target operations",
              file=sys.stderr)
    return code
