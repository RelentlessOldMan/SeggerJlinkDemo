# cmmrun (SeggerJlinkDemo): notes for every session

cmmrun interprets and analyses PRACTICE (`.cmm`) scripts so a SEGGER J-Link can stand in for a
TRACE32 debugger on routine work — flash, peek/poke, reset, run, read a result — without
changing existing scripts. This file is the project's shared brain: the standing rules, how to
work, and where things are. Where the project stands and what comes next is in
[docs/status.md](docs/status.md). Both are in git, so every session on every machine gets the
same picture.

## The brain: what goes in it and what stays private

Claude Code loads this file at the start of every session, on every machine with a clone.
Together with `docs/status.md` it replaces any one session's memory: a session that knows only
what is in git should be able to carry on the work. So:

- **In the brain (this file, `docs/status.md`):** the standing rules and preferences, how to
  build and test, where things are, what is built, what is next, what was learned, what is
  unverified.
- **Never in the repo (brain, code, docs, commit messages, issues):** any employer, workplace,
  customer, vendor, or internal/private-use reference — treat this as a solo, independent,
  public project; anything about a particular machine or person (host names, IPs, user names,
  local paths, **device serial numbers**, COM ports); credentials or tokens.
- **Where private things live:** each machine's own Claude memory or user settings, and the
  gitignored `target.yaml` / `~/.cmmrun/`. When another machine needs one, the user passes it
  over directly; it is never committed.
- **Keeping it current:** update `docs/status.md` with every chunk of work, in the same commit;
  update this file when a rule changes. If a session's private memory holds a project rule
  every session should know, move it here (without the private parts).

## What the project is for

- Let a J-Link replace TRACE32 for routine PRACTICE-script work, so existing `.cmm` scripts run
  unchanged. Subcommands: `scan`, `check`, `inventory`, `probes`, `run`.
- Faithfulness matters: the interpreter must match real TRACE32 behaviour, including its
  quirks. The free TRACE32 Arm simulator is the ground truth — probe it with `tools/t32sim.py`
  rather than guessing at PRACTICE semantics.

## Working through a plan

- When the user says "keep rolling", build task after task until the plan is done or truly
  blocked, committing and pushing each chunk.
- Warn, don't refuse: pick safe defaults and flag the risk rather than stopping.
- Automate: learn what a script collection contains from `cmmrun scan` / `check` output, not by
  asking the user to describe their scripts.

## Code rules

- Python 3.10+ (developed on 3.12). Runtime deps: the standard library, `PyYAML`, and
  `pylink-square` (the J-Link backend, optional `.[jlink]` extra). Don't add others without a
  reason.
- LF line endings everywhere (`.gitattributes` normalizes; still write files with LF to avoid
  churn).
- Tests with `pytest`. The interpreter's behaviour is validated against the TRACE32 simulator:
  `tests/cmm/*.cmm` are run in the simulator and their recorded `*.expected` output must match
  cmmrun line for line. Scripts that need target execution (the free sim can't run code) are
  validated on the mock backend instead and listed in `test_golden.NON_GOLDEN`.

## Git

- Commit and push straight to `main` (solo public repo).
- Before committing: run the tests the change touches (the full suite is ~1.5s, so usually just
  run it all), and run `python tools/name_check.py`. Put the `docs/status.md` update in the same
  commit as the work it describes.
- End commit messages with the `Co-Authored-By:` line the session is told to use.
- **Forbidden-name guard:** `tools/name_check.py` scans tracked files and the last commit
  message against a private word list that lives only on each machine (`~/.cmmrun/private-words.txt`).
  Exit 0 clean, 1 hits (fix before committing), 2 no list on this machine (say so and carry on).

## Commands

    pip install -e .                                   # or .[jlink] for the J-Link backend
    python -m pytest -q                                # full suite, ~1.5s
    python tools/name_check.py                         # forbidden-name guard (before every commit)
    python tools/t32sim.py --t32 <sim dir> script.cmm  # run a script in the TRACE32 simulator (ground truth)
    python tools/t32sim.py --record tests/cmm          # re-record golden output
    cmmrun scan|check <dir> --out <dir>                # analyse a .cmm collection (anonymized report)
    cmmrun run <script> --backend mock|jlink           # run a script (jlink needs --target target.yaml)

Versioning follows the RelentlessOldMan date scheme (`YYYY.M.D.N`), single source of truth
`cmmrun/__init__.py`. (Still on semver `0.2.0`; convert on the next build — see status Next.)

## Where things are

- `README.md`: what it is, usage, the J-Link backend and its safety rules.
- `docs/status.md`: built, next, lessons.
- `docs/practice-semantics.md`: PRACTICE language notes, verified against the simulator.
- `docs/evaluate-scripts.md`: the anonymized script-evaluation workflow + a copy-paste agent prompt.
- `cmmrun/`: the package — `interp/` (interpreter, runner, expressions), `backends/` (base, mock,
  jlink, images), `analysis/` (scan, check, anonymize, report), `lang/` (lexer, parser, vocab),
  `cli.py`. Version in `cmmrun/__init__.py`.
- `tests/cmm/`: golden fixtures (simulator-validated) and example scripts.
- `tools/`: `t32sim.py` (simulator harness), `name_check.py` (name guard), `probe_info.py`,
  `build_vocab.py`.
