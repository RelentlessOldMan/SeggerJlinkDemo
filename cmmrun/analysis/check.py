"""`cmmrun check`: which entry scripts could run, and what blocks the rest.

For every file we collect:
  - unsupported commands and functions (anonymized names)
  - lines whose meaning depends on macros ("dynamic"), and of those, the ones
    whose every possible value is known statically ("resolved")
  - broken things: missing callees, TRACE32 system scripts, parse problems

An entry script is a file that no other file calls. Its verdict combines the
whole call tree below it (worst wins):
  BROKEN > BLOCKED > DYNAMIC > RESOLVED > RUNNABLE
"""
from __future__ import annotations

import fnmatch
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .. import __version__, dataflow
from ..corpus import Corpus, entry_files, resolve_path
from ..lang import lexer
from ..lang.parser import call_target, jump_target, parse_statement, walk
from .anonymize import Anonymizer
from .support import Support, user_commands

ORDER = ["RUNNABLE", "RESOLVED", "DYNAMIC", "BLOCKED", "BROKEN"]
BROKEN_ISSUES = {"unbalanced_close", "unclosed_block", "binary_or_nul_bytes", "unterminated_string",
                 "nesting_too_deep"}


@dataclass
class FileResult:
    blockers: set = field(default_factory=set)     # (kind, display name)
    broken: set = field(default_factory=set)       # reason strings
    dynamic: int = 0
    resolved: int = 0

    def verdict(self) -> str:
        if self.broken:
            return "BROKEN"
        if self.blockers:
            return "BLOCKED"
        if self.dynamic > self.resolved:
            return "DYNAMIC"
        if self.resolved:
            return "RESOLVED"
        return "RUNNABLE"


class HintsError(ValueError):
    pass


def load_hints(path: str | None) -> dict[str, dict[str, set[str]]]:
    """hints.yaml: {"<file glob>": {"macro_name": ["value", ...]}}. Local file, never shared.
    Values are read as plain text (ON stays ON, 0x10 stays 0x10)."""
    if not path:
        return {}
    if isinstance(path, dict):          # already loaded (the CLI loads it before the scan)
        return path
    import yaml
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.load(fh, Loader=yaml.BaseLoader) or {}
    except OSError as e:
        raise HintsError(f"cannot read hints file {path}: {e.strerror or e}") from None
    except yaml.YAMLError as e:
        raise HintsError(f"hints file {path} is not valid YAML: {e}") from None
    if not isinstance(raw, dict) or not all(isinstance(m, dict) or not m for m in raw.values()):
        raise HintsError(f'hints file {path}: expected {{"<file glob>": {{"<macro>": ["value", ...]}}}}')
    out = {}
    for g, m in raw.items():
        out[g] = {}
        for k, v in (m or {}).items():
            vals = v if isinstance(v, list) else [v]
            if not all(isinstance(x, str) for x in vals):
                raise HintsError(f"hints file {path}: values for {g}/{k} must be a list of plain values")
            out[g][str(k).lstrip("&")] = set(vals)
    return out


def _entry_names(pf) -> list[str]:
    for st in pf.stmts:
        if st.kind == "command" and st.canon in ("ENTRY", "PARAMETERS"):
            return [a[1:].strip("()") for a in st.args if a.startswith("&")]
    return []


def _return_literals(pf) -> list[set[str]]:
    """Literal values RETURNed anywhere in a file, by position (flow-insensitive)."""
    out: list[set[str]] = []
    for st in walk(pf.stmts):
        if st.kind == "command" and st.canon in ("RETURN", "ENDDO") and st.args:
            if any("&" in a or "(" in a for a in st.args):
                return []
            for i, a in enumerate(st.args):
                while len(out) <= i:
                    out.append(set())
                out[i].add(a.strip('"'))
    return out


def check(c: Corpus, anon: Anonymizer, backend: str = "jlink", hints: str | None = None) -> dict:
    v = c.vocab
    sup = Support(backend)
    hint_map = load_hints(hints)
    fids = anon.file_ids(c.files)

    # The declaring ON/GLOBALON CMD line is what blocks; its uses are not reported again as unknown.
    user_cmds = user_commands(st for f in c.files.values() if f.pf for st in walk(f.pf.stmts))

    # ---- macro values: per-file flows, then propagate caller arguments/LOCALs to callees
    flows, global_names = {}, {}
    for f in c.files.values():
        if f.pf:
            flows[f.rel] = dataflow.file_flow(f.pf, v)
            for g in flows[f.rel].globals_declared:
                global_names.setdefault(g, set())
    inherited: dict[str, dict[str, set[str]]] = defaultdict(dict)
    envs: dict[str, dict[str, set[str]]] = {}

    def add(rel, name, vals):
        cur = inherited[rel].get(name)
        inherited[rel][name] = (cur | vals) if cur is not None else set(vals)

    for rel, f in c.files.items():
        for g, m in hint_map.items():
            if fnmatch.fnmatch(rel, g) or g == "*":
                for n, vals in m.items():
                    add(rel, n, vals)
        if not f.pf:
            continue
        rets = _return_literals(f.pf)
        # RETURNVALUES / ENTRY after a GOSUB in the same file: literal RETURN values
        prev = None
        for st in walk(f.pf.stmts):
            if st.kind == "command" and rets and (st.canon == "RETURNVALUES" or (
                    st.canon == "ENTRY" and prev is not None and prev.canon == "GOSUB")):
                names = [a[1:].strip("()") for a in st.args if a.startswith("&")]
                for i, n in enumerate(names):
                    if i < len(rets):
                        add(rel, n, rets[i])
            prev = st

    for _ in range(3):  # propagate along call edges a few times (handles chains)
        for rel, f in c.files.items():
            if not f.pf:
                continue
            env = dataflow.value_sets(flows[rel], inherited.get(rel, {}))
            envs[rel] = env
            for e in f.edges:
                if not e.dst or e.dst not in flows or e.stmt is None:
                    continue
                for n, vals in env.items():          # LOCALs are visible in callees
                    if n not in flows[e.dst].defs:
                        add(e.dst, n, vals)
                args = [a for a in e.stmt.args if not lexer.is_option(a)]
                try:
                    after = args[args.index(e.target_text) + 1:]
                except ValueError:
                    after = []
                for n, a in zip(_entry_names(c.files[e.dst].pf), after):
                    vals = dataflow.substitute(a, env, 64)
                    if vals is not None:
                        add(e.dst, n, vals)

    lower_index = {r.lower(): r for r in c.files}

    def judge_stmt(st, rel, res: FileResult):
        if st.kind != "command":
            return
        canon = st.canon
        if st.cmd.upper() in user_cmds and (not st.match or st.match.status != "known"):
            pass
        elif "&" in st.cmd:
            pass  # judged through its resolved values
        elif canon:
            if sup.command(canon) == "unsupported":
                res.blockers.add(("command", canon))
            elif sup.unsupported_event(st):
                ev = sup.unsupported_event(st)
                res.blockers.add(("command", ev if v.is_word(ev.split(" ", 1)[1]) else "ON <other>"))
        else:
            res.blockers.add(("unknown_command", anon.command(st)))
        for fn in st.functions:
            m = v.function(fn)
            if m.status == "partial" and sup.function(m.canon):
                continue    # e.g. OS.FILE(): a real function that is also a name prefix in the vocabulary
            if m.status != "known":
                res.blockers.add(("unknown_function", anon.function(fn)))
            elif not sup.function(m.canon):
                res.blockers.add(("function", m.canon))

    dyn_edges: dict[str, set[str]] = defaultdict(set)   # callees found by resolving dynamic paths
    results: dict[str, FileResult] = {}
    for rel, f in c.files.items():
        res = FileResult()
        results[rel] = res
        if not f.pf:
            res.broken.add("parse_error")
            continue
        for i in f.pf.issues:
            if i in BROKEN_ISSUES:
                res.broken.add(i)
        for e in f.edges:
            if e.status in ("missing", "ambiguous"):
                res.broken.add("missing_script")
            elif e.status == "t32_system":
                res.broken.add("t32_system_script")
        env = envs.get(rel, {})
        for st in walk(f.pf.stmts):
            judge_stmt(st, rel, res)
            serious = [p for p in st.dynamic if p != "args_only"]
            if not serious:
                continue
            res.dynamic += 1
            texts = dataflow.substitute(st.text, env, 64)
            if texts is None:
                continue
            ok = True
            for t in texts:
                s2 = parse_statement(t, st.lineno, v)
                if s2 is None:
                    ok = False
                    break
                judge_stmt(s2, rel, res)
                if "script_path" in serious:
                    status, dst = resolve_path(c, rel, call_target(s2), lower_index)
                    if dst:
                        dyn_edges[rel].add(dst)
                    if status in ("missing", "ambiguous"):
                        res.broken.add("missing_script")
                    elif status == "t32_system":
                        res.broken.add("t32_system_script")
                if "jump_target" in serious:
                    jt = jump_target(s2)
                    if jt not in f.pf.labels and jt not in f.pf.subroutines:
                        res.broken.add("missing_label")
            if ok:
                res.resolved += 1

    # ---- trees
    entries = sorted(entry_files(c, dyn_edges), key=lambda r: fids[r])

    def tree(rel):
        seen, stack, depth = set(), [(rel, 0)], 0
        while stack:
            r, d = stack.pop()
            if r in seen:
                continue
            seen.add(r)
            depth = max(depth, d)
            for dst in {e.dst for e in c.files[r].edges if e.dst} | dyn_edges.get(r, set()):
                if dst in c.files:
                    stack.append((dst, d + 1))
        return seen, depth

    per_entry, entry_blockers = [], {}
    verdict_entry, verdict_all = Counter(), Counter(res.verdict() for res in results.values())
    for rel in entries:
        files, depth = tree(rel)
        agg = FileResult()
        for r in files:
            fr = results[r]
            agg.blockers |= fr.blockers
            agg.broken |= fr.broken
            agg.dynamic += fr.dynamic
            agg.resolved += fr.resolved
        vd = agg.verdict()
        verdict_entry[vd] += 1
        if agg.blockers and not agg.broken:
            entry_blockers[rel] = agg.blockers
        reasons = sorted(agg.broken) + [f"{k}:{n}" for k, n in sorted(agg.blockers)][:8]
        if len(agg.blockers) > 8:
            reasons.append(f"+{len(agg.blockers) - 8} more")
        per_entry.append({"file": fids[rel], "verdict": vd, "files": len(files), "depth": depth,
                          "dynamic_lines": agg.dynamic, "resolved_lines": agg.resolved, "reasons": reasons})

    appears, alone = Counter(), Counter()
    for bl in entry_blockers.values():
        for b in bl:
            appears[b] += 1
        if len(bl) == 1:
            alone[next(iter(bl))] += 1
    blockers = [{"name": n, "kind": k, "appears_in": cnt, "unblocks_alone": alone.get((k, n), 0)}
                for (k, n), cnt in sorted(appears.items(), key=lambda kv: (-alone.get(kv[0], 0), -kv[1], kv[0]))]

    per_entry.sort(key=lambda r: (ORDER.index(r["verdict"]), r["file"]))
    return {
        "schema": "cmmrun-check/1",
        "tool_version": __version__,
        "anonymized": anon.enabled,
        "backend": backend,
        "verdicts": {
            "entry_only": {k: verdict_entry.get(k, 0) for k in ORDER},
            "all_files": {k: verdict_all.get(k, 0) for k in ORDER},
        },
        "blockers": blockers,
        "per_entry_script": per_entry,
    }
