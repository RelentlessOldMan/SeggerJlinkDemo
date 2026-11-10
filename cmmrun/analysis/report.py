"""Render scan/check results as Markdown, and the allowlist used by the leak guard."""
from __future__ import annotations

import re

# Every word the report itself may contain (keys, categories, headings). The
# leak guard redacts any word that is not here, not public vocabulary, and
# not an id. Keep this in sync when adding report fields.
ALLOWED_TERMS = set("""
schema cmmrun scan check tool_version anonymized corpus files duplicates skipped too_large unreadable unreadable_dirs links excluded totals
physical_lines code_lines comment_lines blank_lines continued_lines lines_per_file encodings
ascii utf utf-8 utf-16 cp1252 binary bom line_endings crlf lf cr mixed none issues occurrences
binary_or_nul_bytes unterminated_string unbalanced_close unclosed_block duplicate_label parser_crash
max_block_depth_per_file distinct_macros_per_file commands name uses pct_files status known partial
unknown public_word abbreviated_uses first_args macro other command_groups options functions features
labels subroutine_blocks macro_assignments recursive_assignments blocks macro_expansion_switch_blocks
single_line_if_forms single_line_else_forms single_line_while_forms single_line_repeat_forms
user_defined_commands user defined events registrations actions dynamic
lines_with_any_macro_in_command_args_or_options by_position lines lines_where_meaning_depends_on_a_macro
of_which_all_inputs_literal_in_file of_which_only_literals_or_caller_values origins_by_position
macro_definition_origins top_patterns position origins examples file line_shape sources
subcommand option script_path jump_target args_only
literal caller callee_return target environment time loop global inherited transform_of_unknown
other_function transform not set in this from a
call_graph script_calls_by_kind do run pstep chdir_do dodecrypt on_do globalon_do script_call_resolution
found found_by_name ambiguous missing t32_system temp files_called_by_others entry_scripts
entry_scripts_that_call_others entry_call_depth files_in_call_cycles fan_out fan_in gosub_calls
gosub_unresolved_targets cpu_families cortex_m cortex_r cortex_a classic_arm riscv tricore powerpc
xtensa dsp str num path id ac fmt redacted leak_guard_redactions inherit default continue
execute exec cont
verdicts verdict RUNNABLE RESOLVED DYNAMIC BLOCKED BROKEN blockers unblocks_alone appears_in
reason reasons kind unsupported ignored supported partial_support support_table scripts
missing_script t32_system_script parse_error dynamic_unresolved via chain entry tree
per_entry_script resolved_lines dynamic_lines blocked_by broken_by entry_only all_files
calls callees depth elapsed_s review before sharing it should contain only names counts ids and backend jlink mock missing_label unknown_command
unknown_function function more
""".split())


def _table(rows, cols, headers=None, limit=None):
    headers = headers or cols
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(cols)]
    for r in rows[:limit] if limit else rows:
        out.append("| " + " | ".join(_cell(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out)


def _cell(x):
    if isinstance(x, list):
        x = ", ".join(str(i) for i in x)
    elif isinstance(x, dict):
        x = ", ".join(f"{k}:{v}" for k, v in x.items())
    return str(x).replace("|", r"\|")


def _kv(d: dict) -> str:
    if not d:
        return "(none)"
    return ", ".join(f"`{k}` {v}" for k, v in d.items())


def scan_markdown(r: dict, top: int = 60) -> str:
    c = r["corpus"]
    t = c["totals"]
    L = []
    L.append("# cmmrun scan report")
    L.append("")
    L.append("> Review this report before sharing it. It should contain only public PRACTICE names, counts, and ids.")
    L.append("")
    L.append(f"Tool {r['tool_version']}, schema `{r['schema']}`, anonymized: **{r['anonymized']}**. "
             "Names not in the public PRACTICE vocabulary appear as `u......` ids; files as `F....`.")
    if "leak_guard_redactions" in r:
        L.append(f"Leak guard redactions: {r['leak_guard_redactions']} (0 expected).")
    L.append("")
    L.append("## Corpus")
    L.append(f"- Files: **{c['files']}**, of which {c.get('duplicates', 0)} are byte-identical copies "
             f"(skipped: {_kv(c['skipped'])})")
    L.append(f"- Lines: {t.get('physical_lines', 0)} physical; {t.get('code_lines', 0)} code, "
             f"{t.get('comment_lines', 0)} comment, {t.get('blank_lines', 0)} blank, "
             f"{t.get('continued_lines', 0)} continued")
    L.append(f"- Lines per file: {_kv(c['lines_per_file'])}")
    L.append(f"- Encodings: {_kv(c['encodings'])}; line endings: {_kv(c['line_endings'])}")
    L.append(f"- Max block depth per file: {_kv(c['max_block_depth_per_file'])}")
    L.append(f"- Distinct macros per file: {_kv(c['distinct_macros_per_file'])}")
    if c["issues"]:
        L.append("- Parse issues: " + ", ".join(f"`{k}` {v['occurrences']} in {v['files']} files"
                                                 for k, v in c["issues"].items()))
    L.append("")
    L.append(f"## Commands (top {top} of {len(r['commands'])})")
    L.append("`~` = public word not in the vocabulary list; `<macro>` = name built from a macro.")
    L.append("")
    L.append(_table(r["commands"], ["name", "uses", "files", "pct_files", "status", "abbreviated_uses", "first_args"],
                    ["command", "uses", "files", "% files", "status", "abbrev.", "first argument"], top))
    L.append("")
    L.append("## Command groups")
    L.append(_table(r["command_groups"], ["name", "uses", "files", "pct_files"],
                    ["group", "uses", "files", "% files"], 40))
    L.append("")
    L.append(f"## Options (top 40 of {len(r['options'])})")
    L.append(_table(r["options"], ["name", "uses", "files", "pct_files"], ["option", "uses", "files", "% files"], 40))
    L.append("")
    L.append(f"## Functions (top 50 of {len(r['functions'])})")
    L.append(_table(r["functions"], ["name", "uses", "files", "pct_files"],
                    ["function", "uses", "files", "% files"], 50))
    L.append("")
    L.append("## Language features")
    for k, v in r["features"].items():
        L.append(f"- `{k}`: {v['occurrences']} in {v['files']} files")
    L.append(f"- Event registrations: {_kv(r['events']['registrations'])}")
    L.append(f"- Event actions: {_kv(r['events']['actions'])}")
    L.append("")
    d = r["dynamic"]
    L.append("## Dynamic lines")
    L.append(f"- Lines with a macro in command, arguments or options: {d['lines_with_any_macro_in_command_args_or_options']}")
    L.append(f"- By position: " + ", ".join(f"`{k}` {v['lines']} lines / {v['files']} files"
                                         for k, v in d["by_position"].items()))
    n = d["lines_where_meaning_depends_on_a_macro"]
    L.append(f"- Lines whose **meaning** depends on a macro (command, sub-command, option, script path, jump target): **{n}**")
    if n:
        L.append(f"  - all inputs literal within the file: {d['of_which_all_inputs_literal_in_file']} "
                 f"({100 * d['of_which_all_inputs_literal_in_file'] // n}%)")
        L.append(f"  - only literals or values from callers/globals: {d['of_which_only_literals_or_caller_values']} "
                 f"({100 * d['of_which_only_literals_or_caller_values'] // n}%)")
    L.append("- Origins by position:")
    for p, o in d["origins_by_position"].items():
        L.append(f"  - `{p}`: {_kv(o)}")
    L.append(f"- Where macro values come from (all definitions): {_kv(d['macro_definition_origins'])}")
    L.append("")
    L.append("### Top dynamic patterns")
    for i, p in enumerate(d["top_patterns"], 1):
        L.append(f"{i}. **{p['position']}** from **{p['origins']}**: {p['lines']} lines in {p['files']} files")
        for ex in p["examples"]:
            L.append(f"   - `{ex['line_shape']}` ({ex['file']})")
            for s in ex["sources"]:
                L.append(f"     - `{s}`")
    L.append("")
    g = r["call_graph"]
    L.append("## Call graph")
    L.append(f"- Script calls by kind: {_kv(g['script_calls_by_kind'])}")
    L.append(f"- Call resolution: {_kv(g['script_call_resolution'])}")
    L.append(f"- Entry scripts (not called by any other): {g['entry_scripts']}, of which call others: "
             f"{g['entry_scripts_that_call_others']}; files called by others: {g['files_called_by_others']}")
    L.append(f"- Call depth from entry scripts: {_kv(g['entry_call_depth'])}")
    L.append(f"- Files in call cycles: {g['files_in_call_cycles']}")
    L.append(f"- Fan-out: {_kv(g['fan_out'])}; fan-in: {_kv(g['fan_in'])}")
    L.append(f"- GOSUB calls: {g['gosub_calls']}, unresolved targets: {g['gosub_unresolved_targets']}")
    L.append("")
    L.append("## Target")
    L.append(f"- CPU families (from SYStem.CPU): {_kv(r['target']['cpu_families'])}")
    L.append("")
    return "\n".join(L)


def check_markdown(r: dict, top_blockers: int = 30, list_files: int = 200) -> str:
    L = ["# cmmrun check report", ""]
    L.append("> Review this report before sharing it. It should contain only public PRACTICE names, counts, and ids.")
    L.append("")
    L.append(f"Tool {r['tool_version']}, schema `{r['schema']}`, anonymized: **{r['anonymized']}**.")
    if "leak_guard_redactions" in r:
        L.append(f"Leak guard redactions: {r['leak_guard_redactions']} (0 expected).")
    L.append("")
    L.append("Verdict meanings: RUNNABLE = everything supported; RESOLVED = dynamic lines, but every possible value "
             "is known and supported; DYNAMIC = needs `--allow-dynamic` (values known only at run time); "
             "BLOCKED = uses something unsupported; BROKEN = missing callee, parse problem, or TRACE32 system script.")
    L.append("")
    for scope in ("entry_only", "all_files"):
        L.append(f"## Verdicts ({scope.replace('_', ' ')})")
        L.append(_kv(r["verdicts"][scope]))
        L.append("")
    L.append(f"## Top blockers (entry scripts)")
    L.append("`unblocks alone` = entry scripts that become runnable if only this item is added.")
    L.append("")
    L.append(_table(r["blockers"], ["name", "kind", "appears_in", "unblocks_alone"],
                    ["item", "kind", "blocked entry scripts", "unblocks alone"], top_blockers))
    L.append("")
    L.append("## Per entry script")
    L.append(_table(r["per_entry_script"], ["file", "verdict", "files", "depth", "dynamic_lines", "resolved_lines", "reasons"],
                    ["script", "verdict", "files in tree", "depth", "dynamic", "resolved", "reasons"], list_files))
    L.append("")
    return "\n".join(L)


def safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)
