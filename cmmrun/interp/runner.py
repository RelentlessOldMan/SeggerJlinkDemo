"""`cmmrun run`: pre-flight check, then run a script on a backend.

Pre-flight loads every script the entry script can reach through literal
DO/RUN paths (following CD) and refuses to start if any of them uses a command,
function, debug symbol or `~~` path cmmrun does not support, jumps to a missing
label, or (without --allow-dynamic) builds a command, script path or jump
target from macros. That way a script either runs from start to end or does not
touch the target.

This holds under ON ERROR too: a script that expects ON ERROR to skip an
unknown command is still refused, because cmmrun cannot tell which failures the
author meant to skip, and the target must not be left half-programmed.

Exit codes: 0 ok, 2 script error, 3 target error, 4 timeout, 5 unsupported.
"""
from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass, field

from ..lang.parser import call_target, jump_target, walk
from . import functions
from .expr import tokenize
from .interpreter import Frame, Interpreter, ScriptFailure, Timeout
from .values import PracticeError, TargetError, UnsupportedError

EXIT_OK, EXIT_SCRIPT, EXIT_TARGET, EXIT_TIMEOUT, EXIT_UNSUPPORTED = 0, 2, 3, 4, 5
BLOCKING_DYNAMIC = {"command", "subcommand", "script_path", "jump_target", "option"}
SCRIPT_PROBLEMS = {"parse_error", "missing_label", "missing_script"}     # the script is broken: exit 2
HOST_PATH_COMMANDS = {"OPEN", "APPEND", "CD", "ChDir"}    # DO/RUN paths are checked as call targets
EXPR_COMMANDS = {"IF", "WHILE", "RePeaT", "PRINT"}      # whose arguments are all expressions


def symbols_in(text: str) -> list[str]:
    """Bare identifiers used as values (debug symbols), outside function-call arguments.
    Arguments of functions such as Register(PC) take names, so they are not looked at."""
    try:
        toks = tokenize(text)
    except PracticeError:
        return []
    out, stack = [], []          # stack: True for a function call's parentheses
    for k, t in enumerate(toks):
        nxt = toks[k + 1] if k + 1 < len(toks) else None
        if t.kind == "op" and t.text == "(":
            prev = toks[k - 1] if k else None
            stack.append(bool(prev is not None and prev.kind == "ident" and not t.space_before))
        elif t.kind == "op" and t.text == ")":
            if stack:
                stack.pop()
        elif t.kind == "ident" and not any(stack):
            if nxt is not None and nxt.kind == "op" and nxt.text in ("(", ":") and not nxt.space_before:
                continue
            if t.text.upper() in ("TRUE", "FALSE"):
                continue
            out.append(t.text)
    return out


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
    cwd0 = it.cwd
    try:
        _walk_scripts(it, pf, todo, seen)
    finally:
        it.cwd = cwd0           # CD is followed during the walk only
    return pf


def _walk_scripts(it: Interpreter, pf: Preflight, todo: list, seen: set):
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
                exprs = [st.rhs] if st.kind == "assign" and st.rhs and "&" not in st.rhs else []
                if st.kind == "command" and st.canon in EXPR_COMMANDS and "&" not in " ".join(st.args):
                    exprs.append(" ".join(a for a in st.args if not a.startswith("%")))
                for sym in (s for e in exprs for s in symbols_in(e)):
                    pf.problems.append(Problem(where, "unsupported_symbol",
                                               f"{sym} (debug symbols are not supported; use an address)"))
                if st.kind != "command":
                    continue
                for arg in (st.args if st.canon in HOST_PATH_COMMANDS or st.canon.startswith(("Data.LOAD",
                                                                                         "Data.SAVE")) else []):
                    if arg.strip('"').startswith("~~") and not arg.strip('"').startswith("~~~") and not it.t32_sys:
                        pf.problems.append(Problem(where, "unsupported_path",
                                                   f"{arg}: '~~' is the TRACE32 system directory"))
                dyn = [d for d in st.dynamic if d in BLOCKING_DYNAMIC]
                if dyn:
                    pf.dynamic.append(Problem(where, "dynamic", f"{', '.join(dyn)} in {st.text!r}"))
                    continue
                canon = st.canon
                if not canon or not command_supported(canon):
                    pf.problems.append(Problem(where, "unsupported_command", canon or st.cmd))
                    continue
                from ..analysis.support import Support
                ev = Support.unsupported_event(st)
                if ev:
                    pf.problems.append(Problem(where, "unsupported_command", ev))
                    continue
                if canon in ("GOTO", "GOSUB", "JUMPTO") or (canon == "ON" and jump_target(st)):
                    tgt = jump_target(st)
                    if tgt and "&" not in tgt and tgt not in prog.labels and tgt not in prog.subs:
                        pf.problems.append(Problem(where, "missing_label", tgt))
                if canon in ("CD", "ChDir") and st.args and "&" not in st.args[0]:
                    saved = it.frame
                    try:
                        it.frame = Frame("script", None, prog)      # relative to this script, as when run
                        d = it.resolve_host_path(st.args[0])
                        if d.is_dir():
                            it.cwd = d          # later DO paths in the walk resolve from here
                    except PracticeError:
                        pass
                    finally:
                        it.frame = saved
                if canon in ("DO", "RUN", "ChDir.DO") or (canon == "ON" and call_target(st)):
                    tgt = call_target(st)
                    if tgt and "&" not in tgt:
                        saved = it.frame
                        try:
                            it.frame = Frame("script", None, prog)
                            target = it.resolve_script(tgt, it.frame)
                            todo.append(target)
                            if canon == "ChDir.DO":
                                it.cwd = target.parent
                        except UnsupportedError as e:
                            pf.problems.append(Problem(where, "unsupported_path", str(e)))
                        except PracticeError:
                            pf.problems.append(Problem(where, "missing_script", tgt))
                        finally:
                            it.frame = saved


def make_backend(a):
    if a.backend == "mock" or a.dry_run:
        from ..backends.mock import MockBackend
        return MockBackend()
    try:
        import pylink  # noqa: F401
    except ImportError:
        raise UnsupportedError("the J-Link backend needs pylink-square: pip install cmmrun[jlink]") from None
    from ..backends.jlink import JLinkBackend, load_config
    b = JLinkBackend(load_config(a.target))
    b.check()          # serial, deny list and on-board rule, before any script runs; opens nothing
    return b


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
    except TargetError as e:
        print(f"cmmrun: {e}", file=sys.stderr)
        return EXIT_TARGET
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
        if any(p.kind in SCRIPT_PROBLEMS for p in blockers):
            return EXIT_SCRIPT          # broken (missing label or script, parse error): a script error
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
            sys.stdout.flush()          # before the error goes to stderr
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
