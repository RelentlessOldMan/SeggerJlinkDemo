"""`cmmrun scan`: statistics about a script collection. Anonymized by default."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .. import __version__, dataflow
from ..corpus import Corpus, call_depths, entry_files
from ..lang import lexer
from ..lang.parser import call_target, walk
from .anonymize import Anonymizer
from .support import user_commands

CPU_FAMILIES = [
    (re.compile(r"CORTEX-?M|^CM\d|STM32|NRF5|NRF9|EFR32|EFM32|SAM[DLEC]|ATSAM|LPC|KINETIS|MK\d|"
                r"IMXRT|MIMXRT|RP2040|RP2350|GD32|MSP432|TM4C|XMC|S32K|PSOC|CYT|MAX32|DA14|CC13|CC26|"
                r"RA\d|R7FA|NUC|M\d{3}|APOLLO|SIM3", re.I), "cortex_m"),
    (re.compile(r"CORTEX-?R|^CR\d|TMS570|RM4|RM5|RH850|TCR|ZYNQ.*R5|R52|R82", re.I), "cortex_r"),
    (re.compile(r"CORTEX-?A|^CA\d|ARMV8|ARMV9|A53|A55|A57|A72|A76|IMX[6-9]|ZYNQ|AM3|AM5|AM6|OMAP|"
                r"SNAPDRAGON|EXYNOS|BCM2", re.I), "cortex_a"),
    (re.compile(r"ARM7|ARM9|ARM11|ARM9\d\d|ARM7TDMI|XSCALE", re.I), "classic_arm"),
    (re.compile(r"RISCV|RISC-V|RV32|RV64|ESP32C|GD32V|FE310", re.I), "riscv"),
    (re.compile(r"TC2|TC3|TC4|AURIX|TRICORE", re.I), "tricore"),
    (re.compile(r"PPC|MPC|E200|SPC5|POWER", re.I), "powerpc"),
    (re.compile(r"XTENSA|ESP32|ESP8266", re.I), "xtensa"),
    (re.compile(r"C28|C66|C67|C55|C64|C7X|DSP|HEXAGON|SHARC|CEVA", re.I), "dsp"),
]


def cpu_family(arg: str) -> str:
    if "&" in arg:
        return "dynamic"
    a = arg.strip('"')
    for rx, fam in CPU_FAMILIES:
        if rx.search(a):
            return fam
    return "other"


def _bucket(n: int, edges=(0, 10, 50, 100, 250, 500, 1000, 2500, 5000)) -> str:
    prev = None
    for e in edges:
        if n <= e:
            return f"{prev + 1 if prev is not None else 0}-{e}"
        prev = e
    return f">{edges[-1]}"


def _top(counter: Counter, files: dict, total_files: int, extra=None, limit=None):
    rows = []
    for name, uses in counter.most_common(limit):
        row = {"name": name, "uses": uses, "files": len(files[name]),
               "pct_files": round(100.0 * len(files[name]) / max(1, total_files), 1)}
        if extra:
            row.update(extra(name))
        rows.append(row)
    return rows


def scan(c: Corpus, anon: Anonymizer, max_examples: int = 3) -> dict:
    v = c.vocab
    fids = anon.file_ids(c.files)
    nfiles = len(c.files)

    # ---------------------------------------------------------- corpus facts
    enc, endings, sizes, issues = Counter(), Counter(), Counter(), Counter()
    tot = Counter()
    files_with_issue = defaultdict(set)
    for f in c.files.values():
        endings[f.line_endings] += 1
        if f.pf is None:
            issues["parser_crash"] += 1
            continue
        pf = f.pf
        enc[pf.encoding + ("+bom" if pf.bom else "")] += 1
        sizes[_bucket(pf.n_phys)] += 1
        tot["physical_lines"] += pf.n_phys
        tot["code_lines"] += pf.n_code
        tot["comment_lines"] += pf.n_comment
        tot["blank_lines"] += pf.n_blank
        tot["continued_lines"] += pf.n_continued
        for i in pf.issues:
            issues[i] += 1
            files_with_issue[i].add(f.rel)

    # ---------------------------------------------------------- vocabulary use
    cmd_uses, cmd_files = Counter(), defaultdict(set)
    cmd_abbrev, cmd_status = Counter(), {}
    cmd_first_args: dict[str, Counter] = defaultdict(Counter)
    grp_uses, grp_files = Counter(), defaultdict(set)
    opt_uses, opt_files = Counter(), defaultdict(set)
    fn_uses, fn_files = Counter(), defaultdict(set)
    feat, feat_files = Counter(), defaultdict(set)
    on_events, on_actions = Counter(), Counter()
    user_cmds: set[str] = set()
    cpu = Counter()
    depth_hist = Counter()
    macro_names_per_file = []

    user_cmds |= user_commands(st for f in c.files.values() if f.pf for st in walk(f.pf.stmts))

    def feature(name, rel, n=1):
        feat[name] += n
        feat_files[name].add(rel)

    for f in c.files.values():
        if not f.pf:
            continue
        pf, rel = f.pf, f.rel
        depth_hist[str(min(pf.max_depth, 10)) + ("+" if pf.max_depth >= 10 else "")] += 1
        if pf.labels:
            feature("labels", rel, len(pf.labels))
        if pf.subroutines:
            feature("subroutine_blocks", rel, len(pf.subroutines))
        names = set()
        for st in walk(pf.stmts):
            names.update(st.macros)
            if st.kind == "assign":
                names.add(st.name)
                feature("macro_assignments", rel)
                if st.recursive:
                    feature("recursive_assignments", rel)
                for fn in st.functions:
                    d = anon.function(fn)
                    fn_uses[d] += 1
                    fn_files[d].add(rel)
                continue
            if st.kind == "block_open":
                feature("blocks", rel)
                if st.block_mode:
                    feature("macro_expansion_switch_blocks", rel)
                continue
            if st.kind != "command":
                continue
            if st.inline is not None and st.canon in ("IF", "ELSE", "WHILE", "RePeaT"):
                feature("single_line_" + st.canon.lower() + "_forms", rel)
            if st.cmd.upper() in user_cmds and st.match.status != "known":
                name = "<user-defined command>"
            elif "&" in st.cmd:
                name = anon.command(st)  # contains <macro>
            else:
                name = anon.command(st)
            cmd_uses[name] += 1
            cmd_files[name].add(rel)
            status = st.match.status if st.match else "unknown"
            if status == "partial" and name.endswith("~"):
                status = "public_word"
            cmd_status[name] = status
            if st.match and st.match.abbreviated:
                cmd_abbrev[name] += 1
            grp = name.split(".")[0] if "<macro>" not in name.split(".")[0] else "<macro>"
            grp_uses[grp] += 1
            grp_files[grp].add(rel)
            pos = [a for a in st.args if not lexer.is_option(a)]
            if pos:
                a0 = pos[0]
                if "&" in a0:
                    cmd_first_args[name]["<macro>"] += 1
                elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", a0) and v.is_word(a0):
                    cmd_first_args[name][a0.upper()] += 1
                elif a0.startswith("%"):
                    cmd_first_args[name]["%" + anon.word(a0[1:])] += 1
                else:
                    cmd_first_args[name]["<other>"] += 1
            for o in st.options:
                d = anon.option(o)
                opt_uses[d] += 1
                opt_files[d].add(rel)
            for fn in st.functions:
                d = anon.function(fn)
                fn_uses[d] += 1
                fn_files[d].add(rel)
            canon = st.canon
            if canon == "SYStem.CPU" and pos:
                cpu[cpu_family(pos[0])] += 1
            if canon in ("ON", "GLOBALON") and pos:
                ev = pos[0].upper()
                ev = ev if v.is_word(ev) else "<other>"
                on_events[f"{canon} {ev}"] += 1
                up = [a.upper() for a in pos[1:]]
                act = next((a for a in ("GOSUB", "GOTO", "JUMPTO", "DO", "CONTINUE", "CONT", "INHERIT",
                                        "DEFAULT", "EXECUTE", "EXEC") if a in up), "none" if len(pos) < 2 else "other")
                on_actions[f"{canon} {act}"] += 1
                if ev == "CMD" and len(pos) > 1:
                    feature("user_defined_commands", rel)
        macro_names_per_file.append(len(names))

    # ---------------------------------------------------------- dynamic lines
    global_names: dict[str, set[str]] = {}
    flows = {}
    for f in c.files.values():
        if f.pf:
            ff = dataflow.file_flow(f.pf, v)
            flows[f.rel] = ff
            for g in ff.globals_declared:
                global_names.setdefault(g, set())
    for ff in flows.values():
        dataflow.resolve_origins(ff, global_names)

    dyn_pos, dyn_pos_files = Counter(), defaultdict(set)
    pos_origin: dict[str, Counter] = defaultdict(Counter)
    patterns: Counter = Counter()
    pattern_files = defaultdict(set)
    pattern_examples: dict[tuple, list] = defaultdict(list)
    n_dyn = n_dyn_literal = n_dyn_callers = 0
    assign_origins = Counter()
    for rel, ff in flows.items():
        for name, md in ff.defs.items():
            for k in ff.origins.get(name, md.kinds) or {"literal"}:
                assign_origins[k] += 1
    caller_like = {"literal", "caller", "inherited", "global"}
    for f in c.files.values():
        if not f.pf:
            continue
        ff = flows[f.rel]
        for st in walk(f.pf.stmts):
            if not st.dynamic:
                continue
            serious = [p for p in st.dynamic if p != "args_only"]
            for p in st.dynamic:
                dyn_pos[p] += 1
                dyn_pos_files[p].add(f.rel)
            if not serious:
                continue
            n_dyn += 1
            # macros in the dynamic position(s)
            names = set()
            if {"command", "subcommand"} & set(serious):
                names |= set(lexer.macros_in(st.cmd))
            if "option" in serious:
                names |= {m for o in st.options for m in lexer.macros_in(o)}
            if "script_path" in serious:
                names |= set(lexer.macros_in(call_target(st)))
            if "jump_target" in serious:
                names |= set(lexer.macros_in(st.args[0] if st.args else ""))
            origins = dataflow.origins_of(ff, names, global_names)
            if origins <= {"literal"}:
                n_dyn_literal += 1
            if origins <= caller_like:
                n_dyn_callers += 1
            for p in serious:
                for o in origins:
                    pos_origin[p][o] += 1
            key = ("+".join(sorted(serious)), "+".join(sorted(origins)))
            patterns[key] += 1
            pattern_files[key].add(f.rel)
            if len(pattern_examples[key]) < max_examples:
                mids: dict[str, str] = {}
                shape = anon.stmt_shape(st, mids)
                chain = []
                for n in list(mids)[:4]:
                    md = ff.defs.get(n)
                    if md is None:
                        src = "global" if n in global_names else "not set in this file (from a caller)"
                        chain.append(f"&{mids[n]}: {src}")
                        continue
                    srcs = []
                    for d in md.shapes[:2]:
                        if d.kind == "assign":
                            srcs.append(anon.shape(d.rhs, mids))
                        else:
                            srcs.append(d.canon or "?")
                    if not srcs:
                        srcs = ["<" + "+".join(sorted(md.kinds or {"other"})) + ">"]
                    chain.append(f"&{mids[n]} = " + " | ".join(srcs))
                pattern_examples[key].append({"file": fids[f.rel], "line_shape": shape, "sources": chain})

    # ---------------------------------------------------------- call graph
    edge_kind, edge_status = Counter(), Counter()
    called = set()
    fan_out = Counter()
    gosub_total = gosub_unres = 0
    for f in c.files.values():
        fan_out[_bucket(len({e.dst for e in f.edges if e.dst}), (0, 1, 2, 5, 10, 20, 50))] += 1
        gosub_total += f.gosub_total
        gosub_unres += f.gosub_unresolved
        for e in f.edges:
            edge_kind[e.kind] += 1
            edge_status[e.status] += 1
            if e.dst:
                called.add(e.dst)
    callers_of = c.callers()
    has_out = {f.rel for f in c.files.values() if any(e.dst for e in f.edges)}
    roots = entry_files(c)
    depths, n_cyclic = call_depths(c)
    root_depth = Counter(str(depths.get(r, 0)) for r in roots if r in has_out)
    fan_in = Counter(_bucket(len(callers_of.get(r, ())), (0, 1, 2, 5, 10, 20, 50)) for r in c.files)

    result = {
        "schema": "cmmrun-scan/1",
        "tool_version": __version__,
        "anonymized": anon.enabled,
        "corpus": {
            "files": nfiles,
            "duplicates": c.duplicates,
            "skipped": dict(c.skipped),
            "totals": dict(tot),
            "lines_per_file": dict(sorted(sizes.items(), key=lambda kv: _sort_bucket(kv[0]))),
            "encodings": dict(enc),
            "line_endings": dict(endings),
            "issues": {k: {"occurrences": n, "files": len(files_with_issue[k])} for k, n in issues.items()},
            "max_block_depth_per_file": dict(sorted(depth_hist.items())),
            "distinct_macros_per_file": _hist(macro_names_per_file, (0, 5, 10, 20, 50, 100, 200)),
        },
        "commands": _top(cmd_uses, cmd_files, nfiles, lambda n: {
            "status": cmd_status.get(n, "known"),
            "abbreviated_uses": cmd_abbrev.get(n, 0),
            "first_args": dict(cmd_first_args[n].most_common(6)) if n in cmd_first_args else {},
        }),
        "command_groups": _top(grp_uses, grp_files, nfiles),
        "options": _top(opt_uses, opt_files, nfiles),
        "functions": _top(fn_uses, fn_files, nfiles),
        "features": {k: {"occurrences": feat[k], "files": len(feat_files[k])} for k in sorted(feat)},
        "events": {"registrations": dict(on_events.most_common()), "actions": dict(on_actions.most_common())},
        "dynamic": {
            "lines_with_any_macro_in_command_args_or_options": sum(dyn_pos.values()),
            "by_position": {p: {"lines": n, "files": len(dyn_pos_files[p])} for p, n in dyn_pos.most_common()},
            "lines_where_meaning_depends_on_a_macro": n_dyn,
            "of_which_all_inputs_literal_in_file": n_dyn_literal,
            "of_which_only_literals_or_caller_values": n_dyn_callers,
            "origins_by_position": {p: dict(cnt.most_common()) for p, cnt in pos_origin.items()},
            "macro_definition_origins": dict(assign_origins.most_common()),
            "top_patterns": [
                {"position": k[0], "origins": k[1], "lines": n, "files": len(pattern_files[k]),
                 "examples": pattern_examples[k]}
                for k, n in patterns.most_common(15)
            ],
        },
        "call_graph": {
            "script_calls_by_kind": dict(edge_kind),
            "script_call_resolution": dict(edge_status),
            "files_called_by_others": len(called),
            "entry_scripts": len(roots),
            "entry_scripts_that_call_others": sum(1 for r in roots if r in has_out),
            "entry_call_depth": dict(sorted(root_depth.items(), key=lambda kv: int(kv[0]))),
            "files_in_call_cycles": n_cyclic,
            "fan_out": dict(sorted(fan_out.items(), key=lambda kv: _sort_bucket(kv[0]))),
            "fan_in": dict(sorted(fan_in.items(), key=lambda kv: _sort_bucket(kv[0]))),
            "gosub_calls": gosub_total,
            "gosub_unresolved_targets": gosub_unres,
        },
        "target": {"cpu_families": dict(cpu)},
    }
    return result


def _sort_bucket(b: str) -> int:
    m = re.match(r">?(\d+)", b)
    return int(m.group(1)) + (1 if b.startswith(">") else 0) if m else 0


def _hist(values, edges):
    cnt = Counter(_bucket(x, edges) for x in values)
    return dict(sorted(cnt.items(), key=lambda kv: _sort_bucket(kv[0])))
