import pytest

from cmmrun.lang import lexer
from cmmrun.lang.parser import parse_text
from cmmrun.lang.vocab import load

V = load()


@pytest.mark.parametrize("typed,canon", [
    ("D.LOAD.Elf", "Data.LOAD.Elf"), ("d.s", "Data.Set"), ("SYS.U", "SYStem.Up"), ("sys.m.attach", "SYStem.Mode.Attach"),
    ("RPT", "RePeaT"), ("R.S", "Register.Set"), ("G", "Go"), ("FLASH.ReProgram", "FLASH.ReProgram"),
])
def test_command_abbreviations(typed, canon):
    m = V.command(typed)
    assert m.status == "known" and m.canon == canon


def test_unknown_command():
    assert V.command("PRIN").status == "unknown"


def test_function_names():
    assert V.function("OS.PPD").canon == "OS.PresentPracticeDirectory"
    assert V.function("D.L").canon == "Data.Long"


def test_decode_variants():
    assert lexer.decode("﻿x".encode("utf-8")).bom
    assert lexer.decode("Grüße".encode("cp1252")).encoding == "cp1252"
    assert lexer.decode(b"a\x00b").issues


def test_comments_and_strings():
    code, comment, unterminated = lexer.strip_comment('PRINT "a;b" ; real comment')
    assert code.strip() == 'PRINT "a;b"' and not unterminated
    assert lexer.split_args('"a b" (1 2) /Opt x') == ['"a b"', "(1 2)", "/Opt", "x"]


def test_continuation_and_blocks():
    pf = parse_text('PRINT "a" \\\n "b"\nIF TRUE()\n(\n  PRINT "x"\n)\nlab: PRINT "y"\n', V)
    assert pf.n_continued == 1
    assert pf.max_depth == 1
    assert "lab" in pf.labels
    assert not pf.issues


def test_parse_issues():
    pf = parse_text('(\nPRINT "x\n)\n)\n', V)
    assert "unterminated_string" in pf.issues and "unbalanced_close" in pf.issues


def test_dynamic_positions():
    pf = parse_text("&c Data.Set\nData.&s 0\nDO &p\nGOTO &l\nData.Set &a %Long 1\nData.Set 0 1 /&o\n", V)
    dyn = [s.dynamic for s in pf.stmts if s.kind == "command"]
    assert dyn == [["command"], ["subcommand"], ["script_path"], ["jump_target"], ["args_only"], ["option"]]
