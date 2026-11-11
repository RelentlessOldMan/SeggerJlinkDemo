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


def test_device_prefix_alone_is_a_no_op():
    from cmmrun.lang.parser import parse_bytes
    pf = parse_bytes(b"B::\nB::   \nIF TRUE() B::\nPRINT 1\n")
    assert [st.canon for st in pf.stmts] == ["IF", "PRINT"] and not pf.issues


def test_block_opens_on_the_same_line():
    pf = parse_text("IF 1==1 (\n PRINT 1\n)\nELSE (\n PRINT 2\n)\nWHILE 0==1 (\n)\n", V)
    assert not pf.issues and pf.max_depth == 1
    assert pf.stmts[0].args == ["1==1"] and pf.stmts[0].inline.kind == "block_open"


def test_char_literals_do_not_start_comments_or_strings():
    pf = parse_text("IF STRing.CHAR(&s,0)==';' PRINT \"semi\"\n"
                    "IF STRing.CHAR(&s,0)=='\"' GOTO done ; comment\ndone:\n", V)
    assert not pf.issues
    assert pf.stmts[0].inline.canon == "PRINT"
    assert pf.stmts[1].inline.args == ["done"]


def test_deep_inline_chains_are_an_issue_not_a_crash():
    from cmmrun.lang.parser import parse_bytes
    assert "nesting_too_deep" in parse_bytes(b"IF 1 " * 1200 + b"PRINT 1\n").issues
    assert "nesting_too_deep" in parse_bytes(b"( " * 1200 + b"\n").issues


def test_bom_before_nul_check():
    d = lexer.decode(b"\xef\xbb\xbfPRINT 1\x00\n")
    assert d.text.startswith("PRINT") and "binary_or_nul_bytes" in d.issues
    d = lexer.decode(b"\xff\xfe" + 'PRINT "é"'.encode("utf-16-le"))
    assert d.encoding == "utf-16" and d.text.startswith("PRINT")
    from cmmrun.corpus import _line_endings
    assert _line_endings(b"\xff\xfe" + "A\r\nB\r\n".encode("utf-16-le")) == "crlf"


def test_origins_do_not_depend_on_definition_order():
    from cmmrun import dataflow
    for text in ('&a=OS.ENV("X")\n&b=&a\n&a=&b\n', '&b=&a\n&a=OS.ENV("X")\n&a=&b\n'):
        ff = dataflow.file_flow(parse_text(text, V), V)
        dataflow.resolve_origins(ff, {})
        assert "environment" in ff.origins["b"], ff.origins


def test_substitute_is_one_pass():
    from cmmrun.dataflow import substitute
    env = {"a": {"&b"}, "b": {"X"}}
    assert substitute("&a &b", env, 8) == {"&b X"}
    assert substitute("&b &a", env, 8) == {"X &b"}
    assert substitute("&(a)x", {"a": {"1"}}, 8) == {"1x"}
