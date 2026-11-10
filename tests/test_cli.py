import json

from cmmrun.cli import main

from conftest import CORPUS, PRIVATE_TOKENS


def _script(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="ascii")
    return str(p)


def test_run_missing_script(tmp_path):
    assert main(["run", str(tmp_path / "nope.cmm")]) == 2


def test_run_ok_and_log(tmp_path, capsys):
    s = _script(tmp_path, "ok.cmm", 'SYStem.CPU CortexM4\nSYStem.Up\nData.Set 0x20000000 %Long 0x11223344\n'
                'PRINT "v=" Data.Long(D:0x20000000)\nENTRY &a\nPRINT "a=&a"\nENDDO\n')
    log = tmp_path / "log.json"
    assert main(["run", s, "hi", "--log", str(log)]) == 0
    assert capsys.readouterr().out.splitlines() == ["v=11223344", "a=hi"]
    ops = [e["op"] for e in json.loads(log.read_text())["events"]]
    assert ops[:3] == ["cpu", "connect", "reset"] and "write" in ops


def test_run_target_error_without_connection(tmp_path):
    s = _script(tmp_path, "t.cmm", "Data.Set 0x0 %Long 0x1\nENDDO\n")
    assert main(["run", s]) == 3


def test_run_script_error(tmp_path):
    s = _script(tmp_path, "e.cmm", "PRINT 1/0\nENDDO\n")
    assert main(["run", s]) == 2


def test_run_timeout(tmp_path):
    s = _script(tmp_path, "w.cmm", "WHILE TRUE()\n(\n  WAIT 1.s\n)\n")
    assert main(["run", s, "--timeout", "5"]) == 4


def test_preflight_refuses_unsupported_before_running(tmp_path, capsys):
    s = _script(tmp_path, "u.cmm", 'PRINT "should not print"\nSYStem.Up\nTrace.List\nENDDO\n')
    assert main(["run", s]) == 5
    cap = capsys.readouterr()
    assert "should not print" not in cap.out and "Trace.List" in cap.err


def test_preflight_dynamic_needs_flag(tmp_path):
    s = _script(tmp_path, "d.cmm", '&c="PRINT"\n&c "x"\nENDDO\n')
    assert main(["run", s]) == 5
    assert main(["run", s, "--allow-dynamic"]) == 0


def test_preflight_follows_do_and_checks_labels(tmp_path, capsys):
    _script(tmp_path, "callee.cmm", "GOSUB nowhere\nENDDO\n")
    s = _script(tmp_path, "main.cmm", 'DO "~~~~/callee.cmm"\nENDDO\n')
    assert main(["run", s]) == 5
    assert "nowhere" in capsys.readouterr().err


def _no_private(text: str):
    low = text.lower()
    for tok in PRIVATE_TOKENS:
        assert tok not in low, tok


def test_scan_cli(tmp_path):
    salt = tmp_path / "salt"
    assert main(["scan", str(CORPUS), "--out", str(tmp_path), "--salt-file", str(salt)]) == 0
    md = (tmp_path / "cmmrun-scan.md").read_text(encoding="utf-8")
    js = (tmp_path / "cmmrun-scan.json").read_text(encoding="utf-8")
    assert json.loads(js)["leak_guard_redactions"] == 0
    _no_private(md)
    _no_private(js)


def test_check_cli(tmp_path):
    salt = tmp_path / "salt"
    assert main(["check", str(CORPUS), "--out", str(tmp_path), "--salt-file", str(salt)]) == 0
    md = (tmp_path / "cmmrun-check.md").read_text(encoding="utf-8")
    js = json.loads((tmp_path / "cmmrun-check.json").read_text(encoding="utf-8"))
    assert js["leak_guard_redactions"] == 0
    _no_private(md)
    _no_private(json.dumps(js))


def test_private_map_written_only_on_request(tmp_path):
    salt = tmp_path / "salt"
    pm = tmp_path / "map.json"
    main(["scan", str(CORPUS), "--out", str(tmp_path), "--salt-file", str(salt), "--private-map", str(pm)])
    m = json.loads(pm.read_text())
    assert any("Quuxify" in v or "ZORBLAX" in v for v in m.values())
