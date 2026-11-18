# Status

Where cmmrun stands. Updated with every chunk of work; the standing rules are in
[CLAUDE.md](../CLAUDE.md). Dates are absolute.

## Next

0. **Remaining blockers after report round 2** (2026-11-18; the round-2 report on
   2026.11.17.3 showed 347 of 1122 entry scripts RUNNABLE, up from 123, and 363 BLOCKED, down
   from 612). Still open, by blocked scripts: `InterCom.execute` / `InterCom.WAIT` (talking to
   a second TRACE32 instance; no equivalent yet), `Var.VALUE()`, `Var.Assign`, `Var.IF` and
   the rest of the `Var` family (need ELF/DWARF debug info: a large feature), `Break.Set` and
   `Break.CONFIG.METHOD` (J-Link breakpoints, not started), `TERM.WRITE` (target terminal;
   RTT, not started), `JTAG.PIN`. `Fine` is an unknown command in TRACE32 too (a script bug,
   reported as such). DYNAMIC scripts (350) need `run --allow-dynamic`.
   Unverified: `SYStem.Mode()` after Attach or Prepare on real hardware (the simulator says
   0x0B, as for Up); `OS.NAME()` on non-Windows hosts; `CLOCK.DATE()` day padding for days 1-9.
   Optional speed-up: a lazy `run` parses each line twice (index pass + execution); about
   1.8 MiB/s now (8.3 MiB of `y.create.l` lines in 4.6 s, 68 MiB peak). Measure before changing.
1. **Evaluate a real script collection.** Run `cmmrun check` over a `.cmm` collection per
   [docs/evaluate-scripts.md](evaluate-scripts.md), bring back the anonymized `cmmrun-check.md`,
   and implement the top-ranked unsupported commands/functions (each validated against the
   simulator). Waiting on: the report from a machine that has such a collection.
2. **Hardware bring-up.** Run the example scripts on a real MCU through the J-Link backend
   (`--backend jlink --target target.yaml`). Waiting on: an MCU board on the designated probe.
   (Probe serial and board details stay in machine memory, not here.)

## Built

- **`scan` / `check`** (2026-11-06, sped up 2026-11-10): analyse a collection of `.cmm` scripts
  and report anonymized command/function coverage. `check` ranks unsupported commands by how
  many scripts they block. A leak guard keeps the report shareable (header shows 0 redactions).
- **PRACTICE interpreter + mock backend + `run` pre-flight** (2026-11-10): runs a subset of
  PRACTICE; pre-flight refuses unsupported / dynamic / broken scripts before the target is
  touched. Behaviour validated against the TRACE32 simulator via golden files.
- **J-Link backend safety layer** (2026-11-10): strict config parsing, deny-list, on-board-probe
  refusal, no automatic firmware update, VTref check on every connect, checked run-control and
  transfers, flash commands. Verified against a real probe for enumeration and power reporting;
  full flash/run on hardware is still unverified (no board yet).
- **Review fixes** (2026-11-10): signed 64-bit numbers, host-file errors catchable by
  `ON ERROR`, DO/GOSUB call-depth limit, tighter pre-flight (debug symbols, `~~` paths), and
  more.
- **Example scripts + evaluation docs** (2026-11-12): `peek` / `poke` / `dump` / `loadsave`
  (golden-validated against the simulator) and `run` (reset→go→halt→read, validated on the mock
  backend). `docs/evaluate-scripts.md` documents the anonymized evaluation workflow and a
  copy-paste agent prompt. 123 tests pass.
- **Project brain** (2026-11-16): `CLAUDE.md`, this file, and `tools/name_check.py`.
- **Date-based versioning** (2026-11-16): switched from semver `0.2.0` to `YYYY.M.D.N`
  (`2026.11.16.0`); `pyproject.toml` now reads the version dynamically from
  `cmmrun/__init__.py`, the single source of truth.
- **Feedback round 1** (2026-11-17), from a `check` run over a real collection:
  - Leak guard: dotted names with a digit-led segment (`LOGGER.Mode.64bit`) are no longer
    redacted.
  - The 8 MiB per-file cap is now `--max-file-size`. Skipped files are warned on stderr, listed
    by `inventory`, counted in both reports, and make their callers BROKEN (`too_large_script`).
  - Memory: the parser shares empty fields, interns names and uses regex fast paths, which cut
    the parse to about 0.6x the memory and 0.5x the time. `run` parses scripts over 2 MiB lazily
    (line index plus an LRU of parsed lines): about 2 to 4x the file size instead of 11 to 21x.
    The golden suite runs in both modes.
  - New: `OS.PresentPracticeFile()` (OS.PPF), `sYmbol.CREATE.Label` / `.Done`, `sYmbol.RESet`,
    `sYmbol.EXIST()` and `ADDRESS.OFFSET()`. Pre-flight accepts names the scripts create
    themselves. `sYmbol.EXIST()` after `Data.LOAD.Elf` is refused, because ELF debug symbols
    are not read.
  - Fix: PRINT shows an address with an access class as `C:0x1000` (and ranges as
    `C:0x1000--0x10FF`), as the simulator does; it used to drop the `0x`.

- **Feedback round 1, part 2** (2026-11-17), checked against the simulator unless noted:
  - `PER.Set.Field <addr> %<size> <mask> <value>`: read-modify-write; the value is shifted to
    the mask's lowest set bit. The forms TRACE32 silently ignores (no size, no value) are
    refused, so a register write is never skipped without notice.
  - `PRINTF` with C-style conversions and TRACE32's size rules (no modifier = 64-bit, `l` =
    32-bit unsigned, `h`, `hh`), and its type and argument-count errors.
  - `QUIT [code]` ends the run, and `cmmrun run` exits with the code, as TRACE32 does.
  - `OS.VERSION()` (Windows hosts only; refused elsewhere).
  - `ENTER` reads a line from stdin into macros (mock-tested only: the simulator needs a
    person at the keyboard). End of input is a script error.
  - `PLIST` and `sYmbol.SourcePATH.*` are ignored (GUI and source-view only).
  - Fix: `%CONTinue` appends to the previous output line, as in TRACE32.
  - `SYStem.Mode <mode>` (the spaced form of `SYStem.Mode.Up` etc.) and `SYStem.Mode()`
    (0 down, 0x0B up). `SYStem.CONFIG <key> <value>`: `DEBUGPORTTYPE` is applied; other keys
    are recorded with a warning, since the J-Link finds the debug logic itself.
  - `WRITEB #n <values>` writes raw bytes. `Data` (the dump window) and `WinTABS` are
    ignored.
  - `STOre <file> Win|HISTory` writes a restore script in TRACE32's frame (dated comment,
    `B::`, `ENDDO`), so a later `DO` of it works. Items with debugger state (`SYStem`, `Break`,
    ...) are refused by `check`, pre-flight and the run, since replaying them resets the target.

- **Report round 2** (2026-11-18), checked against the simulator unless noted:
  - `OS.Area`, `OS.Window` (wait for the command) and `OS.Command` (does not wait) run a host
    command through the shell in the current directory. A failing or missing program is not
    an error. The output goes to stderr (TRACE32 shows it in a window, not in the PRINT log).
    `--no-host-commands` turns them off; a `--dry-run` never runs them.
  - `STOre <file> Register` writes `Register.Set <reg> 0x<HEX>` for R0-R14, PC, XPSR, MSP,
    PSP, CONTROL, FAULTMASK, BASEPRI, PRIMASK (nothing while the target is down), so `DO` of
    the file restores them.
  - `sYmbol.Delete <name>` (only symbols already made by `sYmbol.CREATE.Done`, else "symbol
    not found").
  - Functions: `RUN()` (= `STATE.RUN()`), `CLOCK.DATE()` ("18. Nov 2026"), `CLOCK.TIME()`,
    `CLOCK.UNIX()`, `OS.NAME()`, `FILE.EXIST()` (= `OS.FILE()`: an unquoted argument is a
    literal file name, even `OS.PPF()`), `AREA.EXIST()` (case-sensitive; `A000` and
    `AREA.Create`d areas), `OS.FILE.ABSPATH()` (normalized), `InterCom.PING()` (FALSE: no
    other instance).
  - Ignored (GUI or terminal setup): `sYmbol.CASE`, `TERM.METHOD`, `TERM.SIZE`, `TERM.CLOSE`,
    `WinPAGE.Create` / `.Delete` / `.RESet` / `.select` / `.REName`.
  - `Var`, `Var.View`, `Var.Watch`, `Var.AddWatch` (variable windows) show nothing, with a
    warning: TRACE32 errors on an unknown symbol there, and cmmrun has no debug info to check.
  - Vocabulary: `OS.Area`, `RUN()`, `CLOCK.DATE()`, `CLOCK.TIME()`, `CLOCK.UNIX()` added (the
    manuals' extraction missed them).

## Not built

- Real-hardware flash/run validation — needs a board on the designated probe.
- A TRACE32 RCL backend for A/B comparison — optional; the free Arm simulator is unlicensed
  beyond roughly 50 operations after the first Step/Go, so it can't execute programs.

## Lessons

- **PRACTICE numbers are hex by default.** A bare `16` means `0x16` (22); write `16.` for
  decimal. This produced wrong loop bounds in the first draft of the example scripts — cmmrun
  and TRACE32 agreed on the wrong answer, which at least confirmed fidelity.
- **The simulator is ground truth** for PRACTICE semantics; probe it with `tools/t32sim.py`
  instead of guessing. The free Arm sim cannot execute, so run-control is validated on the mock
  backend.
- **Keep secrets out of the repo.** Probe serials and the private corpus live in machine memory
  and gitignored files. The leak guard (reports) and `tools/name_check.py` (commits) are the
  mechanical guards; run the name check before every commit.
- **Golden tests only cover what they print.** PRINT of an address had been wrong (`C:1000`)
  since the start, because no fixture printed one. When adding a value type, add a fixture that
  prints it.
- **Don't copy TRACE32's silent no-ops when they would skip a target write.**
  `PER.Set.Field` with a missing argument does nothing in TRACE32; cmmrun refuses it instead,
  because on hardware a skipped write is worse than an error.
- **Huge generated scripts exist** (millions of lines of register or symbol tables). Keep
  per-statement memory small, and never parse a whole file up front when only running it.
