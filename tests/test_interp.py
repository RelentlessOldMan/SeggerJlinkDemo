"""Interpreter behaviour against the mock target that the TRACE32 golden files can't cover."""
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
