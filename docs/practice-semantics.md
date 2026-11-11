# PRACTICE semantics notes (for the parser and interpreter)

Facts here are **checked in the TRACE32 Arm instruction-set simulator**. The scripts and
their recorded output are in `tests/cmm/`. Use `tools/t32sim.py` to check anything new.
Items marked *(manual)* come from the public PRACTICE manuals and have not been run yet.

## Lexical
- Labels: column 1, end with `:`, case sensitive. A command may follow on the same line.
- Comments: `;` or `//`. A trailing `\` continues the line.
- An optional device prefix, e.g. `B::`, can come before a command.
- Strings: `"..."`, with `""` for a quote inside. Characters: `'A'` (a number, 0x41).

## Numbers and operators
- **Hex by default**: `1F` and `1e3` are hex. `10.` is decimal. `1.5` and `10.0` are floats. `0x` is hex, `0y` is binary.
- A result is decimal if either operand is decimal, otherwise hex (`10.+0x5` gives `15`, decimal).
- Integers are signed 64 bit and wrap: `0x7FFFFFFF+1` gives `80000000`; `~0` and `-1` print as `0FFFFFFFFFFFFFFFF` in hex; `&n=-1` then `&n<0` is TRUE; `0xFFFFFFFFFFFFFFFF+1` gives `0`; `(0x7FFFFFFFFFFFFFFF+1)<0` is TRUE.
- Integer `/` truncates; `%` is modulo. Precedence is as in C (`1+2*3` = 7).
- Ranges: `0x10++0x4` prints `10--14`; `0x10--0x20` prints `10--20`.
- Times: `500.ms` stored in a macro is `0.5000000000s`. The number before a time unit is decimal even without a dot (`10ms` is 0.010 s); `1Fms` is an error (`PAR_109 unexpected character in hex constant`). `1ms/0` is `division by zero`.

## How values print
| Value | PRINT | ECHO | stored in a macro by `&m=` |
|---|---|---|---|
| hex 0x1F | `1F` | `0x1F` | `0x1F` |
| hex 0xAB | `0AB` (leading 0 before a letter) | `0xAB` | `0xAB` |
| hex 0xA | `0A` | `0xA` | `0x0A` (only a single letter digit is padded) |
| decimal 10. | `10` | `10.` | `10.` |
| binary 0y101 | `00000101` (padded to 8 bits) | | |
| float | `1.5`, `5.0`, `33.3333333333333333` (18 digits) | | `1.5` |
| bool | `TRUE ` (trailing space) / `FALSE` | `TRUE()` | `TRUE()` |
| string "a b" | `a b` | `a b` | `a b` (no quotes) |

- Several PRINT arguments are joined with no separator: `PRINT "x" 5 "y"` gives `x5y`.
- A format (`%Decimal`, `%Hex`, `%BINary`) applies to all the values after it.

## Macros
- Plain text replacement before the line runs: in arguments, in strings, in the command name (`&cmd "x"`) and in a sub-command (`STRing.&sub("q")`). `&(a)z` is the bracketed form.
- **Inside a string, the macro text is inserted as characters.** Its own quotes do not end the string: with `&x` = `"arg1"`, `PRINT "s:&x"` prints `s:"arg1"`.
- An undefined macro stays as literal text (`[&undef]` prints `[&undef]`). A declared, empty macro becomes nothing.
- `&q="1+2"` stores `1+2`, so `PRINT &q` prints `3`. `&h="0x10"` then `&h+1` gives `11`.

### Scopes
- A block `( ... )` is a scope for LOCAL and PRIVATE. Both disappear at the end of the block.
- LOCAL is visible in nested blocks, GOSUB subroutines and DO'd scripts.
- PRIVATE is visible in nested blocks only. It is not visible in a GOSUB subroutine.
- GLOBAL is visible everywhere.
- Assigning to a macro nobody declared creates it at the current *call level* (the script or the GOSUB), not in the block. So a macro first assigned inside an IF block is still there after the block, but one first assigned in a subroutine or a DO'd script is gone after it returns.

### Arguments and return values
- ENTRY gets each argument as raw text, quotes kept: `"hello world"`, `0x20`, `abc`.
- PARAMETERS removes the quotes. A missing argument is empty.
- RETURN and ENDDO values are raw text with macros replaced but not evaluated (`RETURN &v+1 "two"` gives `7+1` and `"two"`). The caller picks them up with ENTRY or RETURNVALUES after the GOSUB or DO.
- RETURNVALUES accepts only string literals ("Only string literals allowed here") and removes the quotes. It updates a macro the caller can see.
- ENTRY also updates a visible macro, otherwise it creates one at the call level.
- RETURN in a script started with DO (outside any GOSUB) is an error: `#pra_stackun: PRACTICE stack underflow`.

## Control flow
- `IF c` / `ELSE` / `ELSE IF c`, each with a one-statement body or a block.
- `WHILE c` with a body. `RePeaT n` with a body, or inline: `RePeaT 3. PRINT "x"`.
- `RePeaT` + body + `WHILE c` (no count) is a do-while loop.
- GOSUB to a label (with arguments), GOTO a label, DO a script with arguments.
- `ENTRY &line &x` with arguments `"a b" c` gives `&line` = `"a b"` (the quotes are kept): `&line` is a macro, only `%LINE` takes the whole line.
- `LOCAL &a` then `PARAMETERS &a` fills the local `&a` (prints `[x]` for argument `x`).
- *(cmmrun)* Nesting of DO and GOSUB is limited to 100 levels (`PRACTICE stack overflow`). Host file errors (OPEN, APPEND, Data.SAVE) are script errors that `ON ERROR` catches.
- `ON ERROR GOTO label` catches an unknown command. The message looks like `##cbf_invcomm: unknown command`.
- END ends all scripts. ENDDO returns to the caller. STOP pauses (cmmrun ends with a warning).
- *(manual)* `JUMPTO` clears the stack. ON events other than ERROR (`STOP`, `TIME`, `ALWAYS`, `CMD`) are not supported by cmmrun.

## Functions (checked)
- `STRing.LENgth` returns a decimal number. `STRing.MID("abcdef",1,2)` = `bc`. `STRing.SCAN("abcdef","cd",0)` = `2`.
- `STRing.CUT("abcdef",2)` = `cdef`; with `-2` it is `abcd`. `STRing.CHAR("abc",1)` = `62` (hex).
- `STRing.SPLIT("a,b,c",",",1)` = `b`. `STRing.FIND("abcdef","dx")` is TRUE if any of the characters is found.
- `STRing.SCANAndExtract(s,key,default)`: the key must start a whitespace-separated word, and the result is the rest of that word (`"a=1 b=2"`, `"b="` gives `2`; `"a=1,b=2"`, `"b="` gives the default).
- `FORMAT.STRing("a",5,0x41)` = `aAAAA`: a number as the fill is a character code, taken as a byte.
- `FORMAT.HEX(4,0x1F)` = `001F`. `FORMAT.Decimal(5,42.)` = `   42`. `FORMAT.BINary(8,5)` = `00000101`.
- `CONVert.HEXTOINT` takes a **number**, not a string, and makes it decimal (`0x1F` gives `31`). `CONVert.INTTOHEX(31.)` gives hex `1F`. Neither is a command.
- `OS.FILE(path)` is a bool. `OS.PPD()` is the directory of the running script.

## Target (in the simulator, after `SYStem.CPU CortexM4` and `SYStem.Up`)
- `Data.Set SD:0x1000 %Long 0x12345678`, then `Data.Long` = `12345678`, `Data.Byte(SD:0x1000)` = `78`, `Data.Word(SD:0x1002)` = `1234` (little endian).
- `Data.Set SD:0x1004 %Byte 0x1 0x2 0x3` writes three bytes.
- `%LE` / `%BE` choose the byte order for one Data.Set.
- `Register.Set PC 0x400`, then `Register(PC)` = `400`. `STATE.RUN()` is FALSE while halted.
- Expressions work inside addresses: `Data.Long(SD:&addr+4)`.

## Paths
- `~~~~` is the directory of the running script. `~~` is the TRACE32 system directory (cmmrun refuses it). `~~~` is the temp directory.
- A script path without an extension gets `.cmm`. Relative paths are tried against the working directory, then against the calling script's directory.
