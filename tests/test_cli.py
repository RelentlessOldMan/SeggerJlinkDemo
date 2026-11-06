import json

from cmmrun.cli import main

from conftest import CORPUS, PRIVATE_TOKENS


def test_run_stub():
    assert main(["run", "x.cmm"]) == 5


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
