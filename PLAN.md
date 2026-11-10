# Plan: run unmodified TRACE32 `.cmm` scripts on a SEGGER J-Link

Working name: **cmmrun**. A small Python tool that interprets a routine subset of the Lauterbach PRACTICE script language (`.cmm`) and carries it out on a SEGGER J-Link, or on another backend.

TRACE32, PRACTICE and PowerView are Lauterbach trademarks. This project is independent and is not affiliated with Lauterbach.

## Goal

Show that the routine things a debugger does on a test bench can run on a ~$500 J-Link with the existing `.cmm` scripts left exactly as they are:
- connect and reset
- flash an image
- read and write memory (peek/poke)
- load, run, wait for halt, read a result
- dump memory

Out of scope: trace, multicore, OS awareness, MMU/cache work, and TRACE32's GUI. A script that uses any of these gets a clear "unsupported" error. The interpreter never skips a command silently.

## Options considered

| Option | Changes to `.cmm`? | Needs Lauterbach licence? | Verdict |
|---|---|---|---|
| **A. Interpreter: parse `.cmm`, drive J-Link through pylink** | None, within the supported subset | No | **Chosen.** Full control flow (IF/WHILE/GOSUB/macros), deterministic errors, works in CI |
| B. Transpiler: `.cmm` to J-Link Commander script | None | No | Only works for straight-line scripts. Commander scripts have no IF and can't use a read value, so a script like `IF Data.Long(D:x)==1` can't be translated. Kept as an optional export later |
| C. Real TRACE32 PowerView as a GDB front-end to J-Link GDB Server | None | Yes (GDB front-end licence); unlicensed it runs as a 10-minute demo | Worth a one-hour experiment as a data point. It is not a replacement, because it still needs TRACE32 licences |
| D. Rewrite the scripts in Python | Every script | No | This is what we want to avoid. It is the fallback for scripts outside the subset |

## Architecture

```
 .cmm script ──► lexer/parser ──► interpreter ──► Backend API ──┬─► MockBackend    (in-memory target; unit tests and CI)
                 (abbreviations,   (macros, IF/     attach/reset/│
                  hex-default      WHILE/GOSUB,     halt/go/read/├─► JLinkBackend   (pylink-square + J-Link DLL)
                  numbers,         functions,       write/load/  │
                  ranges)          error codes)     flash/regs   ├─► T32Backend     (lauterbach-trace32-rcl: same script on
                                                                 │                   real TRACE32 or its simulator, for A/B)
       target.yaml: maps SYStem.CPU names to J-Link device names,└─► (later) pyOCD / OpenOCD backend for $15 CMSIS-DAP probes
                    interface/speed, flash handling policy
```

Parts:
1. **Lexer/parser** for the PRACTICE features that routine scripts use:
   - Command abbreviations: the uppercase letters give the shortest form, so `SYStem.Up` can be written `SYS.U`.
   - Commands are case-insensitive.
   - `;` and `//` comments.
   - Numbers are **hex by default**; `10.` is decimal; `0y...` is binary.
   - Time values such as `100.ms` and `5.s`.
   - Access classes such as `D:`, `P:` and `SD:`.
   - Ranges written `a--b` and `a++n`.
   - `&macros`.
2. **Interpreter** for:
   - Blocks: `LOCAL`/`PRIVATE`/`GLOBAL`, `ENTRY`/`PARAMETERS`, `IF`/`ELSE`, `WHILE`, `RePeaT`, `GOTO`, `GOSUB`/`RETURN`, `DO`, `ENDDO`, `STOP`, `ON ERROR`.
   - Functions: `Data.Long()`, `Data.Word()`, `Data.Byte()`, `Register()`, `STATE.RUN()`, `OS.FILE()`, `FORMAT.HEX()` and others.
   - GUI commands (`AREA`, `WinCLEAR`, `SCREEN`) do nothing.
3. **Command table** mapping each PRACTICE command to backend calls:

   | PRACTICE | Backend / J-Link |
   |---|---|
   | `SYStem.CPU`, `SYStem.CONFIG.DEBUGPORTTYPE`, `SYStem.JtagClock` | device, interface and speed from `target.yaml` |
   | `SYStem.Up`, `SYStem.Mode Attach` | `connect` (+ reset/halt) |
   | `SYStem.RESet`, `SYStem.Down` | reset / close |
   | `Data.Set`, `Data.Long()` and similar | `memory_write*`, `memory_read*` |
   | `Register.Set`, `Register()` | `register_write`, `register_read` |
   | `Go`, `Break`, `Step` | `restart`, `halt`, `step` |
   | `WAIT !STATE.RUN() 2.s` | polled halt with timeout |
   | `Break.Set` | hardware breakpoint |
   | `Data.LOAD.Elf`, `.Binary` | RAM load or flash download |
   | `Data.SAVE.Binary` | `savebin` |
   | `FLASH.Create`, `FLASH.TARGET`, `FLASH.ReProgram` | **flash declarations are accepted and ignored.** The J-Link uses SEGGER's own flash algorithms. `ReProgram ALL` … `ReProgram OFF` brackets a flash download. No Lauterbach flash algorithms are used |
   | `PRINT` | stdout and a log |
   | `ENDDO` | exit code |

4. **Pre-flight check (runs before anything touches the target).** Before connecting, the tool parses the whole script and checks every command and option on every branch, following `DO`/`RUN` calls that have literal paths. If anything is unsupported, it lists every problem with its line number and exits with code 5. It never connects.
   - Optionally, `target.yaml` can list commands to ignore. Each one skipped is logged, never silent.
   - Complex scripts use macros heavily, so dynamic lines get their own handling: see "Dynamic scripts" below.
5. **Analysis commands `scan` and `check`**: see "One tool, three commands" below.
6. **Deterministic exit codes** (e.g. 0 pass, 2 script error, 3 target or connect error, 4 timeout, 5 unsupported command), plus a per-run JSON log recording tool, probe and firmware versions.

## One tool, three commands

All three commands share one front end: lexer → parser → canonicalizer → support table → call graph. They never drift apart: if `check` says a script can run, then `run` will accept it.

| Command | Question it answers | Touches hardware? |
|---|---|---|
| `cmmrun scan <dir>` | "What is in this script collection?" Statistics only. The default output is anonymized and safe to share | No |
| `cmmrun check <dir or script>` | "Which scripts can run on the J-Link, and if not, why not?" One verdict per entry script, following its whole call tree | No |
| `cmmrun run <script>` | Runs it. The same check happens first, as the pre-flight gate | Yes |

### `scan` (exploration)
- **Finds** `*.cmm` by default, with `--ext` for others. Reads files leniently: any encoding, and parse errors are recorded instead of stopping the scan.
- **Reports**:
  - number of files, lines, and comment/code ratio
  - each command in canonical form (`SYS.U`, `sys.up` and `SYStem.Up` all count as `SYStem.Up`): total uses, **files using it, out of N**, and its support status
  - functions used in expressions (`Data.Long()`, `STATE.RUN()`…), counted the same way
  - command options (`/Verify`, `/NoCODE`…), counted per command
  - control-flow features (GOSUB, `ON ERROR`, macros, loops), and how many scripts are entry scripts versus subscripts
  - call-graph shape: depth, and how many files call or are called
  - how often lines are dynamic, i.e. need macro expansion before the command is known
  - calls into the TRACE32 install folder (`~~/...`): these scripts depend on Lauterbach-supplied files, which can't be reused
- **Outputs** Markdown and JSON. The JSON uses a stable schema with the tool version, so reports from different collections can be compared or merged.

### Anonymization (default on; `--private` turns it off for local use)
| Data | Shareable report |
|---|---|
| Command, function and option names that are in the public PRACTICE vocabulary | Shown. This is Lauterbach's published language, not your content |
| A token that isn't in the vocabulary (a typo, a proprietary extension, a custom name) | **Not shown.** It is counted as "unknown command #k", and the hash is salted per run |
| File names and paths | Replaced by `F0001…`. The mapping is written to a separate local file, which is never part of the report |
| Arguments: addresses, CPU names, file names in `DO`/`LOAD`, PRINT strings, comments | **Never output.** Only coarse categories, such as "Data.Set: 412 uses, 96 % literal address" |
| `SYStem.CPU` value | Only the core family (Cortex-M / R / A / other), never the part number |

Each report starts with a "review before sharing" note.

### `check` (evaluation)
- Builds the call graph by resolving `DO`/`RUN`/`GOSUB` targets: relative paths, `~~~~/` (the calling script's own folder) and `~~/` (the TRACE32 install folder). If a script uses `CD`, its paths are marked dynamic.
- Entry scripts: by default, every file that no other file calls. They can also be given explicitly.
- One verdict per entry script, covering the whole tree it pulls in:
  - **RUNNABLE**: every command, option and function in the tree is supported.
  - **BLOCKED**: lists each unsupported item with its full chain, e.g. `F0007 → F0012:42 → Trace.Init (unsupported: trace)`.
  - **DYNAMIC**: it would run only with `--allow-dynamic`; the lines involved are listed.
  - **BROKEN**: a missing callee, a parse error, or a dependency on a `~~/` Lauterbach file.
- **Blocker ranking**: "implementing X unblocks N entry scripts", counting only scripts that X alone stands in the way of. This tells us what to build next, and it is safe to share because it contains only counts and public command names.
- `--explain <script>`: prints the call tree with a status on every node, for local use.

### Dynamic scripts (expected to be common)

A line is dynamic when its command, script path or options come from a macro. There are two layers of handling.

**Layer 1: resolve as much as possible before running (static macro tracking).** `check` follows the values macros take without running anything:
- A macro set to a literal (`&s="~~~~/flash.cmm"`) and later used in `DO &s` resolves to that file.
- If different IF branches set the macro to different literals, the result is a **set** of possible values, and every one of them is checked.
- `ENTRY`/`PARAMETERS` values are carried in from each caller's literal `DO` arguments.
- String building with literals (`&a+"_init.cmm"`, `STRing.*` functions) is followed where all inputs are known.
- Scoping (`LOCAL`/`PRIVATE`/`GLOBAL`) is respected.

Verdicts gain one level:

| Verdict | Meaning | Runs by default? |
|---|---|---|
| RUNNABLE | No dynamic lines | Yes |
| **RESOLVED** | Dynamic lines, but every possible value is known and supported | **Yes** |
| DYNAMIC | At least one value is truly unknowable before running: it comes from target memory, a file, `OS.ENV`, the user, or a loop | Only with `--allow-dynamic` |
| BLOCKED / BROKEN | As before. A **known** possible value that is unsupported counts as BLOCKED | No |

**Layer 2: safe execution of what's left (`--allow-dynamic`).**
- Each dynamic line is expanded, then checked **before it runs**. The checker covers the expanded command and, for `DO`, the whole called script tree, before its first line runs.
- An unsupported line triggers a **defined stop**:
  1. halt the core
  2. optionally run a recovery script named in `target.yaml`
  3. exit with code 5
- The log lists every target change made before the stop: memory written, flash programmed, registers set.
- `--dry-run` runs the script against MockBackend, or a recording backend, first. It finds problems on the likely path without touching hardware. This is a filter, not a guarantee, because values read from a real target may send the script down a different path.

**Closing the loop without editing scripts:**
- **Observed values:** each real run (private mode) writes the values that dynamic lines actually took. `check --observed runs/` adds them to the analysis, so over time DYNAMIC scripts become RESOLVED with real evidence.
- **`hints.yaml`:** a side file where a person states facts the analysis can't prove, such as "in F0012 line 42, `&cmd` is one of {Data.Set, Data.Long}". The `.cmm` files themselves stay unchanged.

**`scan` measures how big the problem is, automatically.** No one has to describe their scripts by hand. The anonymized report has a "dynamic patterns" section, worked out from the code:
- **Where the macro sits:**
  - as the command
  - as a sub-command (`Data.&kind`)
  - as an option (`/&opt`)
  - as a script path (`DO &file`, `DO "&dir/x.cmm"`)
  - as a GOTO/GOSUB target
  - inside arguments only (usually harmless)
- **Where the value comes from**, traced back through assignments. Each pattern is counted by origin:
  - literal or constant
  - ENTRY/PARAMETERS from the caller
  - target memory or registers (`Data.Long()`, `Register()`)
  - file read (`READ #`)
  - environment or OS (`OS.ENV()`, `OS.PPD()`, `OS.PWD()`)
  - user input (`ENTER`, `INKEY`, `DIALOG`)
  - loop counter
  - string building (`STRing.*`, concatenation)
  - unknown
- **Control-flow features:**
  - ON ERROR/TIME handlers
  - GLOBALON
  - JUMPTO
  - recursion
  - computed loops
  - CD/ChDir
  - call-depth histogram
- **Layer-1 result:** what fraction of dynamic lines resolve to a finite, known set of values. The rest are ranked by pattern, to show which pattern would unblock the most lines.
- **Anonymized code-shape examples** for each top pattern, e.g. `<cmd> &m1` with `&m1 ← ENTRY ← caller F0007 literal`. No real names or values.

That shows quickly whether layer 1 is enough or whether hints and observed values will carry the load.

### Code layout
```
cmmrun/
  lang/      lexer.py  parser.py  vocab.py  vocab_data.json   (canonical names, abbreviation rules)
  corpus.py                                     (file discovery, DO/RUN/GOSUB resolution, call graph)
  dataflow.py                                   (static macro origins and value sets)
  analysis/  scan.py  check.py  support.py  anonymize.py  report.py
  interp/    values, expr, functions, interpreter, runner
  backends/  base, mock, images (jlink: milestone 3)
  cli.py                                        (cmmrun scan | check | run)
tools/       build_vocab.py (vocabulary from the public manuals)  probe_info.py (read-only J-Link probe info)
tests/       pytest suite + tests/corpus (synthetic messy script collection)
```

The parser has two modes: lenient for `scan` (always finishes and records errors) and strict for `check` and `run`.

## How the demo proves it works

1. **Same script, two engines, same result.** A/B on one public `.cmm` set:
   - (a) TRACE32 **Instruction Set Simulator** (`simarm.zip`, a free download from Lauterbach). Unlicensed, it allows about 50 script commands or API operations after the first Step or Go, and limits larger scripts and remote control. So the A/B tests are kept small: many short scripts, each driven through `lauterbach-trace32-rcl`. Pure-language scripts that never Step or Go may not count against the limit; check this. It is the reference for language behaviour: PRINT output and the final memory and register state.
   - (b) cmmrun on MockBackend.
   - (c) cmmrun on the real board through the J-Link.

   Compare the outputs; the diff should be empty. This needs no Lauterbach hardware.
2. **The hardware demo** (target board + J-Link), run as `cmmrun demo/run_test.cmm` with no changes to the script. It:
   - connects
   - flashes the demo firmware
   - pokes a "test number" into a RAM mailbox
   - runs until a breakpoint or a done-flag
   - peeks the result word
   - prints PASS/FAIL
   - dumps a RAM window to a file

   Then the recovery script: mass erase, reflash, verify.
3. **If a real Lauterbach probe is ever available:** run the same set through T32Backend on the same board and diff flash hash, registers and mailbox. This is the A/B the hardware story ultimately needs.
4. **Soak:** loop flash, run, check a few hundred times and report the failure count and timing.

## Phases

| # | Phase | Needs the J-Link? | Output |
|---|---|---|---|
| 0 | Setup: git repo, Python package skeleton, pytest. Install the J-Link Software Pack (SEGGER download; you accept the licence), Arm GNU Toolchain, `pylink-square` | No | Builds and tests run |
| 1 | Write from scratch the public demo `.cmm` scripts, the language test scripts, and a synthetic "messy collection" (calls, `~~/` references, unknown commands, odd encodings) to test scan/check against | No | `demo/`, `tests/cmm/`, `tests/corpus/` |
| 2 | Lexer, parser, vocabulary and support table; then **`scan`** with anonymization | No | **First usable tool**: can be run on any private collection right away |
| 3 | Call graph, static macro tracking and **`check`** (verdicts incl. RESOLVED, blocker ranking, `--explain`, hints) | No | Runnable/blocked report |
| 3b | Interpreter core, MockBackend and **`run`** | No | Scripts run against a fake target |
| 4 | Install the TRACE32 simulator and T32Backend; A/B the language semantics | No | Diff report, sim vs cmmrun |
| 5 | Plug in the J-Link: manual check with J-Link Commander (connect, halt, `mem32`, `loadfile`), then JLinkBackend | **Yes** | Demo runs on the board |
| 6 | Demo firmware (C, blinky + result mailbox + done-flag breakpoint) and the full demo plus soak | Yes | Demo log, timings, failure count |
| 7 | Optional: pyOCD/OpenOCD backend (shows the same scripts on a $15 CMSIS-DAP probe), Commander export, GDB front-end experiment | Optional | Extras |
| 8 | README, demo GIF/log, publish to GitHub | No | Public repo |

Phases 1–4 can start today, before the J-Link is plugged in. `scan` and `check` come first on purpose: the results from your private scripts then set the order in which commands get implemented in the interpreter, so we build what the scripts actually use.

## Rules

- Only public dev boards and example scripts written from scratch.
- Never copy scripts from the TRACE32 `demo/` directory, and never use Lauterbach flash algorithms.
- Language behaviour is checked against the official PRACTICE manuals and the simulator.
- Pin the J-Link software and pylink versions.
- **Probe selection is always explicit.** The J-Link backend needs a probe serial number in `target.yaml` and refuses to start without one. Never let the J-Link software pick a probe on its own, because other SEGGER probes, such as the J-Link built into a dev kit, may be on the same PC. A `deny_serials` list blocks probes that must never be touched. Probes are listed through the operating system's USB device list, which doesn't open them.

## Open questions

1. Target board: any unlocked Cortex-M board with reachable SWD pins (STM32, nRF52, SAMD, RP2040, ...). Until one is attached, all work is hardware-free.
2. Is a real Lauterbach probe available at any point, or is the simulator the only TRACE32?
3. J-Link model (BASE / PLUS / EDU)? EDU is fine for a hobby demo but not for commercial use.
