from cmmrun.analysis.anonymize import guard
from cmmrun.analysis.check import check
from cmmrun.analysis.report import ALLOWED_TERMS
from cmmrun.analysis.scan import cpu_family, scan


def test_scan_counts(corpus, anon):
    r = scan(corpus, anon)
    names = {c["name"] for c in r["commands"]}
    assert "Data.Set" in names and "FLASH.ReProgram" in names
    assert "<user-defined command>" in names
    assert r["corpus"]["encodings"].get("cp1252") == 1
    assert r["call_graph"]["script_call_resolution"].get("missing") == 1
    assert r["call_graph"]["script_call_resolution"].get("t32_system") == 1
    _, n = guard(r, corpus.vocab, ALLOWED_TERMS)
    assert n == 0


def test_guard_redacts_unknown_words(corpus):
    data, n = guard({"name": "Data.Set Quuxcorp u1a2b3c4d"}, corpus.vocab, ALLOWED_TERMS)
    assert n == 1 and data["name"] == "Data.Set [redacted] u1a2b3c4d"


def test_cpu_family():
    assert cpu_family("STM32F407VG") == "cortex_m"
    assert cpu_family("CortexR5") == "cortex_r"
    assert cpu_family("&cpu") == "dynamic"


def test_check_verdicts(corpus, anon):
    r = check(corpus, anon)
    v = r["verdicts"]["entry_only"]
    assert v["BLOCKED"] == 1 and v["BROKEN"] == 2 and v["RESOLVED"] == 1
    fids = anon.file_ids(corpus.files)
    by = {e["file"]: e for e in r["per_entry_script"]}
    assert by[fids["top/main_flash.cmm"]]["verdict"] == "RUNNABLE"
    assert by[fids["odd/resolved.cmm"]]["verdict"] == "RESOLVED"
    run_all = by[fids["top/run_all.cmm"]]
    assert run_all["files"] == 6        # dynamic DO "&dir/../lib/test_&name.cmm" resolved to three files
    # callees are not entry scripts
    assert fids["lib/test_alpha.cmm"] not in by


def _tree(tmp_path, files):
    from cmmrun.corpus import load_corpus
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return load_corpus(tmp_path, progress=False)


def _check(c):
    from cmmrun.analysis.anonymize import Anonymizer
    an = Anonymizer(c.vocab, b"t" * 16, enabled=False)
    return check(c, an), an


def test_cycles_and_self_calls_have_entries(tmp_path):
    c = _tree(tmp_path, {"self.cmm": "IF 0==1\n  DO self.cmm\nENDDO\n",
                         "a.cmm": "DO b.cmm\nENDDO\n", "b.cmm": "DO a.cmm\nENDDO\n",
                         "top.cmm": "DO c.cmm\nENDDO\n", "c.cmm": "DO top.cmm\nENDDO\n", "boss.cmm": "DO top.cmm\nENDDO\n"})
    r, _ = _check(c)
    entries = sorted(e["file"] for e in r["per_entry_script"])
    assert entries == ["a.cmm", "boss.cmm", "self.cmm"]
    from cmmrun.corpus import call_depths
    depths, cyclic = call_depths(c)
    assert cyclic == 5 and depths["boss.cmm"] == 1


def test_implemented_commands_are_known(tmp_path):
    c = _tree(tmp_path, {"s.cmm": "Register.RESet\nFLASH.CLEAR ALL\nData.LOAD.Srecord x.s19\nY.List\nENDDO\n"})
    r, _ = _check(c)
    reasons = r["per_entry_script"][0]["reasons"]
    assert not any(x.startswith("unknown_command") for x in reasons), reasons


def test_on_events_other_than_error_block(tmp_path):
    c = _tree(tmp_path, {"s.cmm": "ON CMD FOO GOSUB foo\nFOO\nENDDO\nfoo:\nRETURN\n",
                         "e.cmm": "ON ERROR GOTO bad\nENDDO\nbad:\nENDDO\n"})
    r, _ = _check(c)
    by = {e["file"]: e for e in r["per_entry_script"]}
    assert by["s.cmm"]["verdict"] == "BLOCKED" and by["s.cmm"]["reasons"] == ["command:ON CMD"]
    assert by["e.cmm"]["verdict"] == "RUNNABLE"


def test_lowercase_first_letter_abbreviations():
    from cmmrun.lang.vocab import load
    v = load()
    assert v.command("Y.List").canon == "sYmbol.List" and v.command("SYM.List").canon == "sYmbol.List"
    assert v.function("Y.EXIST").canon == "sYmbol.EXIST"
    assert v.command("SYS.U").canon == "SYStem.Up"


def test_every_implemented_command_is_in_the_vocabulary():
    from cmmrun.analysis.support import CONTROL
    from cmmrun.interp.interpreter import COMMANDS, IGNORED
    from cmmrun.lang.vocab import load
    v = load()
    missing = [n for n in set(COMMANDS) | set(IGNORED) | CONTROL
               if not (v.command(n).status == "known" and v.command(n).canon == n)]
    assert not missing
