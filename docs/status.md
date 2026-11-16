# Status

Where cmmrun stands. Updated with every chunk of work; the standing rules are in
[CLAUDE.md](../CLAUDE.md). Dates are absolute.

## Next

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
