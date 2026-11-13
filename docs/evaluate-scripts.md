# Evaluate a script collection (and help improve cmmrun)

cmmrun's `check` and `scan` commands read a collection of PRACTICE (`.cmm`) scripts and
produce an **anonymized** report of which TRACE32 commands and functions the scripts use and
which ones cmmrun does not support yet. Running this over a real collection is the best way to
decide what to add next: `check` ranks the unsupported commands by how many scripts they
block.

You can use the report two ways:

- **See your own coverage** — how many of your scripts would run on cmmrun today, and what
  stands in the way of the rest.
- **Ask for support** — share the anonymized report (only) to request the missing commands,
  or to contribute them.

## The report is anonymized by design

The report contains command and function **shapes and counts only**. It strips every
argument, string, comment and path, replaces identifiers with placeholders, and reports
`SYStem.CPU` only as a core family. A final **leak guard** checks every word against an
allowlist and replaces anything unexpected with `[redacted]`; the report header shows the
count, which should be **0**.

Rules worth keeping whoever runs it:

- Share only `cmmrun-check.md` (and `cmmrun-scan.md` if asked). Never share raw `.cmm` files.
- The `--private` and `--private-map` flags produce an un-redacted name map for your own use.
  The files they write are named `*-PRIVATE.*`. Keep those on your machine; never share them.
- Confirm the report header shows `leak_guard_redactions: 0` before sharing. If it is not 0,
  report the number (not the report) as a bug.

## Quick version (just the commands)

On the machine that holds the scripts, with Python 3.10+:

```
git clone https://github.com/RelentlessOldMan/SeggerJlinkDemo
cd SeggerJlinkDemo
pip install -e .                               # no [jlink] extra: this is static analysis, no hardware

cmmrun check "/path/to/your/scripts" --out cmmrun-reports
cmmrun scan  "/path/to/your/scripts" --out cmmrun-reports   # optional: command/function frequencies
```

Then open `cmmrun-reports/cmmrun-check.md`, confirm `leak_guard_redactions: 0`, and read the
verdict summary and the ranked blocker list. `check` defaults to the `jlink` support table
(real-hardware target); add `--backend mock` to check against the mock backend instead.

## Driving it with an AI coding agent

If you run an agent (such as Claude Code) on the machine that holds the scripts, paste the
prompt below. It keeps the agent from ever echoing script contents, and has it hand back only
the anonymized report. Fill in the one path at the top.

```text
I need you to run a static-analysis tool over a collection of PRACTICE (.cmm) scripts on
this machine and give me back ONLY an anonymized report. The scripts and their contents are
sensitive and must not leave this machine. Read all the rules before doing anything.

SCRIPT COLLECTION: <PUT THE FULL PATH TO THE .cmm SCRIPTS HERE>

THE TOOL: "cmmrun", a Python CLI at https://github.com/RelentlessOldMan/SeggerJlinkDemo .
Its `check` and `scan` commands produce an ANONYMIZED report: command/function shapes and
counts only, with every argument, string, path, comment and identifier stripped or
salted-hashed. A built-in "leak guard" replaces anything unexpected with [redacted] and
prints the count in the report header.

HARD RULES — do not break these:
1. Never print, paste, quote, summarize, or copy the contents of any .cmm script into your
   replies, your scratchpad, or any file you show me. Not even one line.
2. Never pass the --private or --private-map flags. Never open, read back, or show me any
   file whose name contains "PRIVATE".
3. The ONLY thing you hand back to me is the anonymized report file cmmrun-check.md (and
   cmmrun-scan.md if I ask). Nothing else.
4. Before treating any report as shareable, open it and confirm its header shows
   leak_guard_redactions: 0. If it is NOT 0, STOP, tell me only the number, and do not show
   me the report — it's a tool bug to fix first.
5. Do not push, upload, commit, or send anything anywhere. Local work only.

STEPS:
1. Check Python is 3.10+ (`python --version`).
2. If the tool isn't already here, clone and install it in its own folder (NOT inside the
   script collection):
      git clone https://github.com/RelentlessOldMan/SeggerJlinkDemo
      cd SeggerJlinkDemo
      pip install -e .
3. Run, pointing at the script collection path above:
      cmmrun check "<PATH>" --out cmmrun-reports
      cmmrun scan  "<PATH>" --out cmmrun-reports
   (check defaults to the "jlink" support table — leave it unless told otherwise.)
4. Open cmmrun-reports/cmmrun-check.md and confirm leak_guard_redactions: 0 (rule 4).
5. If it's 0: show the FULL text of cmmrun-check.md inline. In particular the verdict summary
   (how many scripts are RUNNABLE / RESOLVED / DYNAMIC / BLOCKED / BROKEN) and the ranked
   blocker list (which unsupported commands/functions block the most scripts, and which are
   the SOLE blocker of a script). This file is already anonymized by design — showing it is
   fine; showing raw scripts (rules 1–3) is not.

Tell me what you find. The goal is a prioritized list of what to implement next in cmmrun so
more of these scripts become runnable.
```

## What the report tells you

`check` gives every entry script (a file no other file calls) the worst verdict in its call
tree:

| Verdict | Meaning |
|---|---|
| RUNNABLE | everything used is supported |
| RESOLVED | has dynamic lines, but every possible value is known statically and is supported |
| DYNAMIC | needs `run --allow-dynamic`: some values are known only at run time |
| BLOCKED | uses an unsupported command or function |
| BROKEN | missing callee or label, parse problem, or a TRACE32 install-dir script (`~~/...`) |

It then ranks blockers — "command X appears in N blocked scripts and is the only blocker in M
of them" — which is the backlog, most valuable first. See the `README.md` sections
**What `scan` reports** and **What `check` reports** for the full field list.
