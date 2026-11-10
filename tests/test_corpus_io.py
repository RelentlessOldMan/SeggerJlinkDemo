"""File discovery and reading on big trees: copies, excludes, links, inventory."""
import os
import pathlib

import pytest

from cmmrun.analysis.anonymize import guard
from cmmrun.cli import main
from cmmrun.corpus import discover, inventory, load_corpus
from cmmrun.lang.vocab import load

A = b'PRINT "a"\nDO b.cmm\n'
B = b'PRINT "b"\nENDDO\n'


def _tree(root: pathlib.Path):
    for d in ("one", "two", "old/three"):
        (root / d).mkdir(parents=True)
        (root / d / "a.cmm").write_bytes(A)
        (root / d / "b.cmm").write_bytes(B)
    (root / "one" / "notes.txt").write_bytes(b"x")
    (root / "two" / "c.CMM").write_bytes(b'PRINT "c"\n')


def test_copies_are_parsed_once_and_shared(tmp_path):
    _tree(tmp_path)
    c = load_corpus(tmp_path)
    assert sorted(c.files) == ["old/three/a.cmm", "old/three/b.cmm", "one/a.cmm", "one/b.cmm",
                               "two/a.cmm", "two/b.cmm", "two/c.CMM"]
    assert c.duplicates == 4
    assert c.files["one/a.cmm"].pf is c.files["two/a.cmm"].pf
    # edges are still per file: each a.cmm calls the b.cmm next to it
    assert [e.dst for e in c.files["two/a.cmm"].edges] == ["two/b.cmm"]
    assert [e.dst for e in c.files["one/a.cmm"].edges] == ["one/b.cmm"]


def test_exclude_by_name_and_path(tmp_path):
    _tree(tmp_path)
    found, skipped = discover(tmp_path, (".cmm",), excludes=("old",))
    assert {f[1] for f in found} == {"one/a.cmm", "one/b.cmm", "two/a.cmm", "two/b.cmm", "two/c.CMM"}
    assert skipped["excluded"] == 1
    found, _ = discover(tmp_path, (".cmm",), excludes=("two/*.cmm",))
    assert not any(f[1].startswith("two/") for f in found)


def test_size_from_listing_and_too_large(tmp_path, monkeypatch):
    import cmmrun.corpus as corpus
    _tree(tmp_path)
    (tmp_path / "one" / "big.cmm").write_bytes(b";" * 100)
    monkeypatch.setattr(corpus, "MAX_BYTES", 50)
    found, skipped = discover(tmp_path, (".cmm",))
    assert skipped["too_large"] == 1
    assert {f[2] for f in found} == {len(A), len(B), 10}


def test_junctions_are_not_followed(tmp_path):
    _tree(tmp_path)
    link = tmp_path / "one" / "loop"
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(tmp_path), str(link))
        else:
            os.symlink(tmp_path, link, target_is_directory=True)
    except (OSError, AttributeError):
        pytest.skip("cannot create a junction or symlink here")
    found, skipped = discover(tmp_path, (".cmm",))
    assert len(found) == 7 and skipped["links"] == 1


def test_parallel_and_serial_agree(tmp_path):
    _tree(tmp_path)
    a = load_corpus(tmp_path, jobs=1)
    b = load_corpus(tmp_path, jobs=8)
    assert list(a.files) == list(b.files)


def test_inventory(tmp_path, capsys):
    _tree(tmp_path)
    r = inventory(tmp_path)
    assert r["files"] == 7 and r["distinct_files"] == 3
    assert r["bytes"] == 3 * (len(A) + len(B)) + 10
    assert r["distinct_bytes"] == len(A) + len(B) + 10
    assert main(["inventory", str(tmp_path), "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "7 files" in out and "distinct content: 3 files" in out


def test_guard_still_redacts_and_keeps_ids():
    v = load()
    data, n = guard({"files": "F0001 u1a2b3c Data.Set SYS.U secretname m7"}, v, {"files"})
    assert data["files"] == "F0001 u1a2b3c Data.Set SYS.U [redacted] m7" and n == 1
