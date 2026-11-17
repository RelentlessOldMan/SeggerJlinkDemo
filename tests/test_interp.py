"""Interpreter behaviour against the mock target that the TRACE32 golden files can't cover."""
import sys

import pytest

from cmmrun.backends.mock import MockBackend
from cmmrun.interp.interpreter import Interpreter, ScriptFailure


def run(tmp_path, text: str, **kw):
    s = tmp_path / "s.cmm"
    s.write_text(text, encoding="utf-8")
    it = Interpreter(MockBackend(), cwd=str(tmp_path), timeout=kw.pop("timeout", 30), **kw)
    it.run(s)
    return it


def test_flash_create_erase_and_program(tmp_path):
    it = run(tmp_path, "SYStem.Up\n"
                       "FLASH.RESet\n"
                       "FLASH.Create 1. 0x08000000--0x08000FFF 0x400 TARGET Long\n"
                       "FLASH.Erase 0x08000000--0x0800000F\n"
                       "PRINT %Long Data.Long(P:0x08000000)\n"
                       "FLASH.Program ALL\n"
                       "Data.Set P:0x08000000 %Long 0x12345678\n"
                       "FLASH.Program OFF\n"
                       "PRINT %Long Data.Long(P:0x08000000)\n"
                       "ENDDO\n")
    b = it.backend
    assert b.flash_ranges == [(0x08000000, 0x08000FFF)]
    assert it.out_lines == ["0FFFFFFFF", "12345678"]
    ops = [e.op for e in b.events]
    assert ops.index("flash_begin") < ops.index("flash_end")


def test_flash_create_needs_a_range(tmp_path):
    with pytest.raises(ScriptFailure, match="address range"):
        run(tmp_path, "SYStem.Up\nFLASH.Create 1. TARGET\nENDDO\n")



def fails(tmp_path, text, match):
    with pytest.raises(ScriptFailure, match=match) as ei:
        run(tmp_path, text)
    return ei.value.exc


# Each of these used to end in a Python traceback.
@pytest.mark.parametrize("text,match", [
    ('OPEN #1 "no/such/dir/x.txt" /Write\nENDDO\n', "No such file"),
    ('APPEND "no/such/dir/x.txt" "x"\nENDDO\n', "No such file"),
    ('SYStem.Up\nData.SAVE.Binary "no/such/x.bin" 0x0--0xF\nENDDO\n', "No such file"),
    ("PRINT 1Fms\nENDDO\n", "hex constant"),               # TRACE32: PAR_109
    ("PRINT 1ms/0\nENDDO\n", "division by zero"),          # TRACE32: PAR_233
    ("GOSUB f\nENDDO\nf:\nGOSUB f\nRETURN\n", "stack overflow"),
    ("DO s.cmm\nENDDO\n", "stack overflow"),
    ("SYStem.Up\nData.Set 0x10--0x0 %Byte 1\nENDDO\n", "empty address range"),
])
def test_clean_script_errors(tmp_path, text, match):
    fails(tmp_path, text, match)


def test_host_file_errors_reach_on_error(tmp_path):
    it = run(tmp_path, 'ON ERROR CONTinue\nOPEN #1 "no/such/dir/x.txt" /Write\nPRINT "after"\nENDDO\n')
    assert it.out_lines == ["after"]


def test_repeat_count_error_reaches_on_error(tmp_path):
    it = run(tmp_path, 'ON ERROR CONTinue\nRePeaT "x" PRINT 1\nPRINT "after"\nENDDO\n')
    assert it.out_lines == ["after"]


# Behaviour checked in the TRACE32 simulator.
def test_numbers_are_signed_64_bit(tmp_path):
    it = run(tmp_path, '&n=-1\nIF &n<0\n  PRINT "neg"\nPRINT -1\nPRINT 0xFFFFFFFFFFFFFFFF+1\n'
                       'PRINT (0x7FFFFFFFFFFFFFFF+1)<0\nENDDO\n')
    assert it.out_lines == ["neg", "0FFFFFFFFFFFFFFFF", "0", "TRUE "]     # the trailing space is TRACE32's


def test_entry_macro_named_line_and_parameters_after_local(tmp_path):
    it = run(tmp_path, 'GOSUB f "x"\nGOSUB g "a b" c\nENDDO\n'
                       'f:\nLOCAL &a\nPARAMETERS &a\nPRINT "[&a]"\nRETURN\n'
                       'g:\nENTRY &line &x\nPRINT "[&line][&x]"\nRETURN\n')
    assert it.out_lines == ["[x]", '["a b"][c]']


def test_format_string_fill_is_a_byte(tmp_path):
    it = run(tmp_path, 'PRINT FORMAT.STRing("a",5,0x41)\nPRINT FORMAT.STRing("a",3,0x141)\nENDDO\n')
    assert it.out_lines == ["aAAAA", "aAA"]


def test_wait_honours_the_timeout(tmp_path):
    from cmmrun.interp.interpreter import Timeout
    with pytest.raises(ScriptFailure) as ei:
        run(tmp_path, "WAIT 100000.s\nENDDO\n", timeout=1)
    assert isinstance(ei.value.exc, Timeout)


def test_debugport_type(tmp_path):
    it = run(tmp_path, "SYStem.CONFIG.DEBUGPORTTYPE JTAG\nSYStem.JtagClock CTCK 10MHz\nENDDO\n")
    assert it.backend.options == {"interface": "jtag", "clock": "CTCK 10MHz"}
    fails(tmp_path, "SYStem.CONFIG.DEBUGPORTTYPE CJTAG\nENDDO\n", "only SWD and JTAG")


# ---- pre-flight (cmmrun run)
def _run_cli(tmp_path, text, capsys, *extra):
    from cmmrun.cli import main
    s = tmp_path / "m.cmm"
    s.write_text(text, encoding="utf-8")
    code = main(["run", str(s), *extra])
    return code, capsys.readouterr().err


@pytest.mark.parametrize("text,kind", [
    ("SYStem.Up\nPRINT Data.Long(D:0x0)+my_var\nENDDO\n", "unsupported symbol"),
    ("SYStem.Up\nIF counter==1\n  PRINT 1\nENDDO\n", "unsupported symbol"),
    ('SYStem.Up\nData.LOAD.Elf "~~/demo/x.elf"\nENDDO\n', "unsupported path"),
    ('SYStem.Up\nPRINT Var.STRing(x)\nENDDO\n', "unsupported function"),
])
def test_preflight_refuses_before_touching_the_target(tmp_path, capsys, text, kind):
    code, err = _run_cli(tmp_path, text, capsys)
    assert code == 5 and kind in err


def test_preflight_allows_names_inside_functions(tmp_path, capsys):
    code, err = _run_cli(tmp_path, 'IF TRUE()&&(1==1)\n  PRINT STRing.UPpeR("a") FORMAT.HEX(4,0x1F)\nENDDO\n',
                         capsys)
    assert code == 0, err


def test_preflight_follows_cd(tmp_path, capsys):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "callee.cmm").write_text("PRINT 1\nENDDO\n")
    code, err = _run_cli(tmp_path, 'CD sub\nDO callee.cmm\nENDDO\n', capsys)
    assert code == 0, err


def test_run_control(tmp_path):
    # tests/cmm/run.cmm exercises reset -> go-to-address -> wait-for-halt -> read.
    # The free simulator can't execute, so it is validated here on the mock backend.
    import pathlib
    src = (pathlib.Path(__file__).parent / "cmm" / "run.cmm").read_text(encoding="utf-8")
    it = run(tmp_path, src)
    assert it.out_lines == [
        "sp    :20010000",     # initial SP from vector[0]
        "pc    :8000400",      # reset handler from vector[1], thumb bit cleared
        "halted:TRUE ",
        "stop  :TRUE ",        # core halted after Go + WAIT
        "atpc  :8000450",      # stopped at the Go target address
        "r0    :2A",
    ]
    ops = [e.op for e in it.backend.events]
    assert ops.index("reset") < ops.index("go")      # reset before run


def test_preflight_allows_symbols_the_script_creates(tmp_path, capsys):
    code, err = _run_cli(tmp_path, "sYmbol.CREATE.Label buf 0x2000\nsYmbol.CREATE.Done\n"
                                   "PRINT buf+4 \" \" ADDRESS.OFFSET(buf)\nENDDO\n", capsys)
    assert code == 0, err
    code, err = _run_cli(tmp_path, "sYmbol.CREATE.Label buf 0x2000\nsYmbol.CREATE.Done\n"
                                   "PRINT other\nENDDO\n", capsys)
    assert code == 5 and "other" in err


def test_symbol_exist_after_elf_load_is_unsupported(tmp_path):
    it = Interpreter(MockBackend())
    it.elf_loaded = True                # TRACE32 would know the ELF's symbols; cmmrun does not
    s = tmp_path / "m.cmm"
    s.write_text("PRINT sYmbol.EXIST(main)\nENDDO\n")
    with pytest.raises(ScriptFailure, match="debug symbols"):
        it.run(s)


def test_enter_reads_a_line_and_fails_at_end_of_input(tmp_path):
    import io
    s = tmp_path / "m.cmm"
    s.write_text('LOCAL &a &b\nloop:\nENTER &a &b\nPRINT "got:&a/&b"\nGOTO loop\n')
    it = Interpreter(MockBackend())
    it.stdin = io.StringIO('GPIOA "two words"\nRCC\n')
    with pytest.raises(ScriptFailure, match="ENTER: no input"):
        it.run(s)
    assert it.out_lines == ['got:GPIOA/"two words"', "got:RCC/"]


# The simulator rejects each of these (STDPAR_101 wrong result type, STDPAR_100 expression
# expected, cbf_nomorearg); they are script errors that ON ERROR can catch.
@pytest.mark.parametrize("line,match", [
    ('PRINTF "%s" 0x12', "wrong result type"),
    ('PRINTF "%d" "abc"', "wrong result type"),
    ('PRINTF "%c" "A"', "wrong result type"),
    ('PRINTF "%d %d" 1.', "expression expected"),
    ('PRINTF "%d" 1. 2.', "no more arguments"),
])
def test_printf_errors(tmp_path, line, match):
    from cmmrun.interp.values import PracticeError
    assert isinstance(fails(tmp_path, line + "\nENDDO\n", match), PracticeError)


@pytest.mark.skipif(sys.platform != "win32", reason="OS.VERSION() is only known for Windows hosts")
def test_os_version_on_windows(tmp_path):
    it = run(tmp_path, 'PRINT OS.VERSION(0) " " OS.VERSION(1) " " OS.VERSION(4)\nENDDO\n')
    from cmmrun.interp.values import Num, plain
    assert it.out_lines == [f"2 {plain(Num(sys.getwindowsversion().major))} 0"]    # 10 prints 0A


@pytest.mark.parametrize("line,match", [
    ("PER.Set.Field SD:0x100 0xFF00 0xAB", "only <address>"),         # TRACE32: silently nothing
    ("PER.Set.Field SD:0x100 %Long 0xFF00", "only <address>"),        # TRACE32: silently nothing
    ("PER.Set.Field SD:0x100 %Long 0xFF00 0xAB /Verify", "too many keywords"),
])
def test_per_set_field_refuses_forms_that_would_skip_the_write(tmp_path, line, match):
    it_text = "SYStem.Up\nData.Set SD:0x100 %Long 0x12345678\n" + line + "\nENDDO\n"
    fails(tmp_path, it_text, match)


# Simulator: QUIT ends the script and TRACE32 exits with the code (QUIT -> 0, QUIT 0x10 -> 16).
@pytest.mark.parametrize("body,code", [("QUIT", 0), ("QUIT 3", 3), ("QUIT 0x10", 16), ("&c=7\nQUIT &c", 7)])
def test_quit_sets_the_exit_code(tmp_path, capsys, body, code):
    got, err = _run_cli(tmp_path, f'PRINT "before"\n{body}\nPRINT "after"\n', capsys)
    assert got == code, err
    it = run(tmp_path, f'PRINT "before"\n{body}\nPRINT "after"\n')
    assert it.out_lines == ["before"] and it.quit_code == code
