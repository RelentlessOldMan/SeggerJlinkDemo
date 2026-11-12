# SeggerJlinkDemo 🔧

**Run unmodified Lauterbach PRACTICE (`.cmm`) scripts on a SEGGER J-Link, and find out up front which of your scripts can.** The tool is called `cmmrun`: a Python interpreter for a subset of PRACTICE with pluggable backends, plus `scan` and `check` commands that analyse a whole script collection before anything touches hardware.

TRACE32, PRACTICE and PowerView are trademarks of Lauterbach GmbH. This is an independent project, not affiliated with Lauterbach or SEGGER.

## Status (v0.2.0)

| Command | State |
|---|---|
| `cmmrun scan <dir>` | **Works.** Anonymized statistics about a script collection |
| `cmmrun check <dir>` | **Works.** A verdict per entry script and its call tree, plus a ranking of what blocks the rest. It checks against the interpreter's own command and function tables, so `check` and `run` agree |
| `cmmrun run <script> --backend mock` | **Works.** Runs on a simulated target (memory, registers, run state, virtual clock). `--dry-run` forces the mock target, and `--log` writes every target operation as JSON |
| `cmmrun run <script> --backend jlink --target target.yaml` | **Safety layer done, not yet tried on a board.** Probe selection, deny list, on-board refusal, no firmware updates and the VTref check are tested on real hardware; connect, memory, registers, run control and flash are tested against a fake probe |
| `cmmrun probes` | **Works.** Lists SEGGER probes from the OS USB device list without opening any |

`run` first checks every script the entry script can reach (following `CD`). Anything unsupported (a command, a function, a debug symbol, a `~~` TRACE32 system path), a missing label or script, or a line built from macros (unless `--allow-dynamic` is given) stops it before the target is touched. Exit codes: 0 ok, 2 script error (including a missing label or script found by the check), 3 target error, 4 timeout, 5 unsupported.

The interpreter's behaviour is checked against the real thing: the scripts in `tests/cmm/` were run in the free TRACE32 instruction-set simulator, and their recorded output (`*.expected`) must match cmmrun's line for line.

### Example scripts

`tests/cmm/` holds small scripts for the routine debugger workflows, each one runnable with `cmmrun run <script> --backend mock` (or `--backend jlink` on real hardware):

| Script | Shows | Checked against |
|---|---|---|
| `peek.cmm` | read memory (long/word/byte) and registers, formatted | TRACE32 simulator |
| `poke.cmm` | write memory and registers, then read back to verify | TRACE32 simulator |
| `dump.cmm` | hex + ASCII dump of a memory region to the log | TRACE32 simulator |
| `loadsave.cmm` | save a region to a host file, reload it elsewhere, verify | TRACE32 simulator |
| `run.cmm` | reset to the vector table, run to an address, read result | mock backend (the free simulator cannot execute) |

Note the PRACTICE gotcha these lean on: numbers are **hex by default**, so a loop bound like `16` means `0x16` (22). Write `16.` for decimal. See [docs/practice-semantics.md](docs/practice-semantics.md).

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

Each command writes Markdown and JSON reports: `reports/cmmrun-scan.md`/`.json` and `reports/cmmrun-check.md`/`.json`. Nothing is modified and nothing touches hardware.

Options:
- `--ext .cmm .inc`: which extensions to read (default `.cmm`).
- `--private`: real names, for your own use. The output files get a `-PRIVATE` suffix. **Never share them.**
- `--private-map map.json`: writes a local id → real-name table, so you can look up what `u3fa9c10e` or `F0042` refers to. **Never share it.**
- `check --hints hints.yaml`: known values for macros the analysis can't work out, e.g. `{"*": {"cmd": ["Data.Set"]}}`.
- `--exclude GLOB ...`: skip directories or files by name or relative path, e.g. `--exclude old '*/archive/*'`.
- `--jobs N`: parallel directory listings and file reads (default 16).
- `--quiet`: no progress lines on stderr.

### Big trees and network shares

Start with `inventory`. It lists and hashes the scripts without parsing them, and prints how much distinct script text there is, a rough memory and time estimate for `scan`/`check`, and the top-level directories that hold the most. Its directory list shows real names, so it is for your eyes only.

```powershell
cmmrun inventory Z:\scripts
cmmrun scan      Z:\scripts --exclude old '*/archive/*' --jobs 32 --out reports
```

What makes big trees cheap:
- Directory listings and file reads run in parallel, so network round trips overlap. With 5 ms of added latency per operation, 8,000 files take 61 s with `--jobs 1` and 4.6 s with the default 16.
- File sizes come from the directory listing; there is no extra `stat` per file.
- Byte-identical copies (branches, releases, archives) are parsed once and shared. The report counts them as `duplicates`.
- Links and junctions are never followed, so a link loop can't trap the walk. Version-control and virtualenv directories are skipped.
- Unreadable files and directories are counted in the report, not silently dropped.

Measured on a generated test tree of 40,000 files (mostly copies): about 10 s for `scan` and 11 s for `check`. Distinct script text costs about 3.5 s and 60 MiB of memory per MiB, so a few hundred MiB of distinct scripts is fine on a normal PC; for more, split the run with `--exclude` or by subdirectory.

### What the anonymized report contains

- Command, function and option names **only if they are in the public PRACTICE vocabulary** (extracted from Lauterbach's published manuals; see `tools/build_vocab.py`).
- Every other name (custom commands, typos, labels, file names) becomes a salted id such as `u3fa9c10e`. Files become `F0001`, `F0002`, … The salt is random per machine (`~/.cmmrun/salt`), so the ids mean nothing elsewhere.
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

It also ranks blockers: "this command appears in N blocked scripts and is the only blocker in M of them" (adding it moves those M out of BLOCKED, though some may then be DYNAMIC).

## The J-Link backend and its safety rules

Needs `pip install -e .[jlink]` and the J-Link Software Pack from SEGGER. Copy [examples/target.example.yaml](examples/target.example.yaml) to `target.yaml` (gitignored), fill in the probe serial from `cmmrun probes`, then:

```powershell
cmmrun run my_script.cmm --backend jlink --target target.yaml --log runs\log.json
```

Other SEGGER probes, such as the J-Link built into a dev kit, may be plugged into the same PC, so:

- **The probe is always chosen by serial number.** Without one, `run` stops. Nothing lets the J-Link software pick a probe.
- **Deny list.** `deny_serials` in `target.yaml` and in `~/.cmmrun/probes.yaml` (machine-wide) lists probes that are never opened. A denied serial is refused before the J-Link DLL is even loaded.
- **Probes are listed through the OS USB device list**, which doesn't open them. The chosen serial must be present.
- **On-board J-Links are refused** unless `probe.allow_onboard: true`: a probe that appears as a USB composite device (dev-kit J-Links usually add a virtual COM port), or whose product name says `OB`.
- **Config values are read as text**, so a serial with leading zeros such as `000123456789` is the decimal number `cmmrun probes` shows, never an octal number. A malformed `target.yaml` or `probes.yaml` stops the run (exit 3).
- **No firmware updates.** Automatic probe firmware updates are disabled before the probe is opened. (pylink's `disable_dialog_boxes()` is not used, because it sends `SilentUpdateFW`.)
- **Never unsecures a locked chip**, which would mass-erase it.
- **An unpowered target is a clean exit 3** (VTref below `min_vtref_mv`). VTref is checked every time the script connects.
- **Flash:** between `FLASH.ReProgram ALL` (or `FLASH.Program`, `FLASH.AUTO`) and `... OFF`, writes inside the `FLASH.Create` ranges (or the device's flash, from the J-Link device database) are collected and programmed in one J-Link flash download. Writes elsewhere, e.g. to RAM, happen at once.
- `SYStem.CONFIG.DEBUGPORTTYPE SWD|JTAG` and `SYStem.JtagClock` override `interface` and `speed` from `target.yaml`. A clock without a unit is in Hz, as in TRACE32; `RTCK` means adaptive clocking.
- `run` checks the whole script tree before it touches the target. Unsupported means exit code 5, and a broken script exit code 2, with no connection made.

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
