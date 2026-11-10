# SeggerJlinkDemo 🔧

**Run unmodified Lauterbach PRACTICE (`.cmm`) scripts on a SEGGER J-Link, and find out up front which of your scripts can.** The tool is called `cmmrun`: a Python interpreter for a subset of PRACTICE with pluggable backends, plus `scan` and `check` commands that analyse a whole script collection before anything touches hardware.

TRACE32, PRACTICE and PowerView are trademarks of Lauterbach GmbH. This is an independent project, not affiliated with Lauterbach or SEGGER.

## Status (v0.2.0)

| Command | State |
|---|---|
| `cmmrun scan <dir>` | **Works.** Anonymized statistics about a script collection |
| `cmmrun check <dir>` | **Works.** A verdict per entry script and its call tree, plus a ranking of what blocks the rest. It checks against the interpreter's own command and function tables, so `check` and `run` agree |
| `cmmrun run <script> --backend mock` | **Works.** Runs on a simulated target (memory, registers, run state, virtual clock). `--dry-run` forces the mock target, and `--log` writes every target operation as JSON |
| `cmmrun run <script> --backend jlink` | Not yet (milestone 3: J-Link backend) |

`run` first checks every script the entry script can reach. Anything unsupported, a missing label, or a line built from macros (unless `--allow-dynamic` is given) stops it before the target is touched. Exit codes: 0 ok, 2 script error, 3 target error, 4 timeout, 5 unsupported.

The interpreter's behaviour is checked against the real thing: the scripts in `tests/cmm/` were run in the free TRACE32 instruction-set simulator, and their recorded output (`*.expected`) must match cmmrun's line for line.

See [PLAN.md](PLAN.md) for the design and [docs/practice-semantics.md](docs/practice-semantics.md) for the language notes.

## Install

Needs Python 3.10 or newer. No probe and no TRACE32 install needed for `scan` or `check`.

```powershell
git clone https://github.com/RelentlessOldMan/SeggerJlinkDemo.git
cd SeggerJlinkDemo
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e .[dev]
python -m pytest -q             # optional: about 1 s
```

## Analyse a script collection

```powershell
cmmrun scan  C:\path\to\scripts --out reports
cmmrun check C:\path\to\scripts --out reports
```

Each command writes Markdown and JSON reports: `reports/cmmrun-scan.md`/`.json` and `reports/cmmrun-check.md`/`.json`. Nothing is modified and nothing touches hardware. About 3,000 files take 10 to 30 seconds.

Options:
- `--ext .cmm .inc`: which extensions to read (default `.cmm`).
- `--private`: real names, for your own use. The output files get a `-PRIVATE` suffix. **Never share them.**
- `--private-map map.json`: writes a local id → real-name table, so you can look up what `u3fa9c1` or `F0042` refers to. **Never share it.**
- `check --hints hints.yaml`: known values for macros the analysis can't work out, e.g. `{"*": {"cmd": ["Data.Set"]}}`.

### What the anonymized report contains

- Command, function and option names **only if they are in the public PRACTICE vocabulary** (extracted from Lauterbach's published manuals; see `tools/build_vocab.py`).
- Every other name (custom commands, typos, labels, file names) becomes a salted id such as `u3fa9c1`. Files become `F0001`, `F0002`, … The salt is random per machine (`~/.cmmrun/salt`), so the ids mean nothing elsewhere.
- No arguments, addresses, strings, comments or paths. Code shapes use placeholders, e.g. `Data.Set <ac>:&m1 %Long <num>`.
- `SYStem.CPU` appears only as a core family (`cortex_m`, `cortex_r`, …).
- A final **leak guard** checks every word in the report against an allowlist and replaces anything unexpected with `[redacted]`. The report header shows the count, which should be 0. If it isn't, please report it as a bug (without the report).

Read the report before you share it anyway.

### What `scan` reports

- Corpus: files, lines, encodings, line endings, parse issues, block depth.
- Commands, command groups, options and functions: uses, files using each, abbreviated uses, the kind of first argument.
- Language features: labels, subroutines, blocks, single-line IF/RePeaT, `ON`/`GLOBALON` events and actions, user-defined commands.
- **Dynamic lines**: where a macro decides the command, sub-command, option, script path or jump target, and where that macro's value comes from: literal, caller, callee return, target memory, file, environment, time, user input, loop, global. Each top pattern comes with anonymized example shapes.
- Call graph: how `DO` targets resolve (found / missing / TRACE32 system dir `~~/`), entry scripts, depth, fan-in and fan-out, cycles.

### What `check` reports

For every entry script (a file no other file calls), the worst verdict in its call tree:

| Verdict | Meaning |
|---|---|
| RUNNABLE | everything used is supported |
| RESOLVED | has dynamic lines, but every possible value is known statically and is supported |
| DYNAMIC | needs `run --allow-dynamic`: some values are known only at run time |
| BLOCKED | uses an unsupported command or function |
| BROKEN | missing callee or label, parse problem, or a TRACE32 install-dir script (`~~/...`) |

It also ranks blockers: "this command appears in N blocked scripts and alone blocks M of them."

## Safety rules for the J-Link backend (milestone 3)

- The probe is always chosen by serial number. Nothing ever auto-selects a probe, and a `deny_serials` list blocks probes that must never be opened.
- Automatic J-Link firmware updates are disabled before a probe is opened.
- `run` checks the whole script tree before it touches the target. Unsupported means exit code 5, with no connection made.

## Development

```powershell
python -m pytest -q
python tools/build_vocab.py <dir of pdftotext outputs of the public PRACTICE manuals>
python tools/t32sim.py --record tests/cmm      # re-record golden output (needs the TRACE32 simulator)
```

`tools/t32sim.py` runs a script headless in the TRACE32 Arm simulator (`t32marm`) and captures what it prints. Use it to settle any question about PRACTICE behaviour: write a small script, run it, and add it to `tests/cmm/` with its recorded output.

`tests/corpus/` is a synthetic, deliberately messy script collection: odd encodings, broken blocks, dynamic `DO` paths, unknown commands. Every script in this repository was written from scratch for it.

## Contributing

This is a personal tool, published as-is — **issues and pull requests aren't accepted** (PRs auto-close). Fork it and make it your own. 🔧

## License

MIT — see [LICENSE](LICENSE). © 2026 RelentlessOldMan.
