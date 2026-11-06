# PRACTICE semantics notes (for the parser and interpreter)

Facts checked against the public PRACTICE manuals. Things marked TODO still need
checking in the manual or the simulator.

## Lexical
- Labels: column 1, end with `:`, case sensitive. A command may follow on the same line (`myLabel: ENTER &a`).
- Comments: `;` or `//`. A trailing `\` continues the line (no whitespace after it).
- An optional device prefix, e.g. `B::`, can come before a command.
- Numbers: **hex by default** (`5` = 0x5). A trailing dot means decimal (`5.`). `0x` = hex, `0y` = binary.
  Source: PARAMETERS example (`"5"` prints as hex, `"5."` as decimal).
- Time literals: `500ms`, `1.s`, `5.0s`.
- Strings: `"..."`. Characters: `'X'`.

## Macros
- Text replacement over the whole line before it is interpreted. Some commands (ENTRY, ...) are exceptions.
- `&name`, or the bracketed form `&(name)`. `&&name=` is a recursive assignment.
- Scopes:
  - LOCAL: visible in GOSUB, DO and sub-blocks.
  - PRIVATE: visible only in its own block and sub-blocks.
  - GLOBAL: visible everywhere.
  - Assigning to a macro that does not exist creates it as a LOCAL.
- ENTRY: reuses existing macros from callers. A macro that doesn't exist is created automatically.
- PARAMETERS: creates PRIVATE macros for any name not in scope. The caller's LOCALs are always out of scope for it. Arguments are quoted strings.
- RETURN / ENDDO take optional values. The caller fetches them with ENTRY or RETURNVALUES.

## Control flow
- Blocks are `( ... )`. A one-line body may omit the brackets. `IF c ( step )` on a single line is NOT allowed.
- `IF c` / `ELSE` / `ELSE IF c`.
- Loops: `WHILE c`, `RePeaT n [cmd]` (`n=0` loops forever), `RePeaT <n> ... WHILE`. TODO: check the RePeaT forms.
- Labels and subroutines share one namespace. GOSUB targets either a label or a SUBROUTINE name.
  - `SUBROUTINE name` followed by a `( )` block.
- `GOTO label | line | &macro`. You can jump out of a block, but not into one.
- `JUMPTO` clears the stack. Its target must be a top-level label in a stacked module.
- ON:
  - Events: `ERROR | STOP | TIME t | ALWAYS | CMD name`.
  - Actions: `inherit | CONTinue | DEFault | DO f | GOSUB/GOTO/JUMPTO label-or-inline-block`.
  - An ON lives in the stack level that sets it.
- END ends all scripts and closes files. ENDDO returns to the caller.
- STOP pauses (interactive). In batch mode, treat it as a defined stop.

## Output
- PRINT writes data without decoration (hex shown without `0x`). ECHO adds decoration (`0x42`, `23.`, `TRUE()`).
- PRINT format options: `%Hex %Decimal %BINary %Ascii %String`, plus `%ERROR %WARNING`, `%CONTinue`, `COLOR.x`.

## TODO
- Rules for abbreviated command names: confirm against the simulator.
- Operator precedence and the range syntax (`--`, `++`, `..`) (ide_user.pdf, "Operators").
- Data.Set `%Long`/`%Word`/`%Byte`, `/Verify`, Data.Long() and similar functions.
- WAIT forms (`WAIT 100ms`, `WAIT !STATE.RUN() 1.s`).
- `~~` (system dir), `~~~` (temp dir), `~~~~` (script dir). OS.PPD().
