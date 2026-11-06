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
    data, n = guard({"name": "Data.Set Quuxcorp u1a2b3c"}, corpus.vocab, ALLOWED_TERMS)
    assert n == 1 and data["name"] == "Data.Set [redacted] u1a2b3c"


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
