"""J-Link backend safety rules and operations, against a fake pylink JLink (no hardware)."""
import types

import pytest

pytest.importorskip("pylink")

from cmmrun.backends import jlink as J  # noqa: E402
from cmmrun.cli import main  # noqa: E402
from cmmrun.interp.values import TargetError  # noqa: E402

GOOD, KIT = 111222333, 444555666
PROBES = [J.Probe(GOOD, 0x1020, "J-Link", False), J.Probe(KIT, 0x1060, "USB Composite Device", True)]


class FakeExc(Exception):
    pass


class FakeJLink:
    def __init__(self, vtref=3300, product="J-Link V11", serial=None):
        self.calls, self.vtref, self.product, self.serial = [], vtref, product, serial
        self.mem, self.halted_ = {}, True
        self.regs = {0: 0, 13: 0x20001000, 15: 0x08000100}

    def exec_command(self, c): self.calls.append(("exec", c))
    def open(self, serial_no): self.calls.append(("open", serial_no)); self.serial = self.serial or serial_no
    def close(self): self.calls.append(("close",))
    serial_number = property(lambda s: s.serial)
    product_name = property(lambda s: s.product)
    firmware_version = "fake fw"
    hardware_status = property(lambda s: types.SimpleNamespace(voltage=s.vtref))
    def set_tif(self, t): self.calls.append(("tif", t)); return True
    def connect(self, dev, speed): self.calls.append(("connect", dev, speed))
    def register_list(self): return [0, 13, 15]
    def register_name(self, i): return {0: "R0", 13: "R13 (SP)", 15: "R15 (PC)"}[i]
    def register_read(self, i): return self.regs[i]
    def register_write(self, i, v): self.regs[i] = v
    def memory_read8(self, a, n): return [self.mem.get(a + i, 0) for i in range(n)]
    def memory_write8(self, a, d): self.mem.update({a + i: b for i, b in enumerate(d)})
    def flash(self, data, addr): self.calls.append(("flash", addr, bytes(data))); self.memory_write8(addr, list(data))
    def reset(self, halt): self.calls.append(("reset", halt))
    def breakpoint_set(self, a, thumb): self.calls.append(("bp", a)); return 7
    def breakpoint_clear(self, h): self.calls.append(("bpclr", h))
    def restart(self): self.halted_ = False; self.calls.append(("go",))
    def halted(self): return self.halted_
    def halt(self): self.halted_ = True
    def step(self): self.calls.append(("step",))


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("CMMRUN_HOME", str(tmp_path / "home"))
    box = {"jl": FakeJLink(), "made": 0}

    def make(cfg):
        box["made"] += 1
        return box["jl"], FakeExc
    monkeypatch.setattr(J, "_make_jlink", make)
    monkeypatch.setattr(J, "list_probes", lambda: list(PROBES))
    return box


def cfg(**kw):
    c = J.JLinkConfig(serial=GOOD, device="STM32F407VG", source="t.yaml")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_serial_is_required(fake):
    with pytest.raises(TargetError, match="must name the probe"):
        J.JLinkBackend(cfg(serial=None)).connect("up")
    assert fake["made"] == 0


def test_denied_probe_is_never_opened(fake):
    with pytest.raises(TargetError, match="deny list"):
        J.JLinkBackend(cfg(deny_serials={GOOD})).connect("up")
    assert fake["made"] == 0


def test_user_deny_file_applies(fake, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "probes.yaml").write_text(f"deny_serials: [{GOOD}]\n")
    t = tmp_path / "target.yaml"
    t.write_text(f"probe: {{serial: {GOOD}}}\n")
    assert GOOD in J.load_config(t).deny_serials


def test_onboard_probe_refused_unless_allowed(fake):
    with pytest.raises(TargetError, match="built into a dev kit"):
        J.JLinkBackend(cfg(serial=KIT)).connect("up")
    assert fake["made"] == 0
    J.JLinkBackend(cfg(serial=KIT, allow_onboard=True)).connect("up")


def test_absent_probe(fake):
    with pytest.raises(TargetError, match="not connected"):
        J.JLinkBackend(cfg(serial=999)).connect("up")


def test_firmware_update_disabled_before_open(fake):
    J.JLinkBackend(cfg()).connect("up")
    calls = fake["jl"].calls
    execs = [c[1] for c in calls[:calls.index(("open", GOOD))] if c[0] == "exec"]
    assert execs[0] == "DisableAutoUpdateFW"
    assert not any(c[0] == "exec" and c[1] in J.NEVER_SEND for c in calls)


def test_onboard_product_name_refused_after_open(fake):
    fake["jl"].product = "J-Link OB-SAM3U128-V2-NordicSemi"
    with pytest.raises(TargetError, match="on-board"):
        J.JLinkBackend(cfg()).connect("up")
    assert fake["jl"].calls[-1] == ("close",)


def test_serial_mismatch_after_open(fake):
    fake["jl"].serial = KIT
    with pytest.raises(TargetError, match="expected"):
        J.JLinkBackend(cfg()).connect("up")


def test_unpowered_target_is_target_error(fake):
    fake["jl"].vtref = 0
    b = J.JLinkBackend(cfg())
    with pytest.raises(TargetError, match="not powered"):
        b.connect("up")
    assert not any(c[0] == "connect" for c in fake["jl"].calls) and not b.connected


def test_operations(fake):
    b = J.JLinkBackend(cfg())
    b.set_option("clock", "10.MHz")
    b.connect("up")
    assert ("connect", "STM32F407VG", 10000) in fake["jl"].calls
    b.write("D", 0x20000000, b"\x01\x02")
    assert b.read("D", 0x20000000, 3) == b"\x01\x02\x00"
    assert b.read_reg("pc") == 0x08000100 and b.read_reg("R13") == 0x20001000
    b.write_reg("SP", 0x20002000)
    assert b.read_reg("sp") == 0x20002000
    with pytest.raises(TargetError):
        b.read_reg("nosuch")
    b.go(until=0x08000200)
    assert b.is_running()
    fake["jl"].halted_ = True
    assert not b.is_running() and ("bpclr", 7) in fake["jl"].calls
    b.close()
    assert not b.connected


def test_flash_writes_are_merged(fake):
    b = J.JLinkBackend(cfg())
    b.connect("up")
    b.flash_begin()
    b.write("P", 0x08000000, b"\xAA\xBB")
    b.write("P", 0x08000002, b"\xCC")
    b.write("P", 0x08001000, b"\xDD")
    assert not fake["jl"].mem
    b.flash_end()
    assert [c for c in fake["jl"].calls if c[0] == "flash"] == [
        ("flash", 0x08000000, b"\xAA\xBB\xCC"), ("flash", 0x08001000, b"\xDD")]


def test_parse_pnp():
    lines = ["USB\\VID_1366&PID_1060&MI_00\\7&1&0&0000\tPorts\tJLink CDC UART Port (COM5)",
             "USB\\VID_1366&PID_1060\\000444555666\tUSB\tUSB Composite Device",
             "USB\\VID_1366&PID_1020\\000111222333\tUSBDevice\tJ-Link"]
    assert J.parse_pnp(lines) == PROBES


def test_khz():
    assert J._khz("4MHz") == 4000 and J._khz("500kHz") == 500 and J._khz("auto") == "auto"
    assert J._khz("1000000.") == 1000                   # SYStem.JtagClock without a unit is Hz
    assert J._khz("1000", default_unit="khz") == 1000   # target.yaml speed: is kHz
    assert J._khz("RTCK") == "adaptive" and J._khz("CTCK 10MHz") == 10000
    for bad in ("1Hz", "100MHz", "fast"):
        with pytest.raises(TargetError):
            J._khz(bad)


def test_run_cli_unpowered_exit_3(fake, tmp_path, capsys):
    fake["jl"].vtref = 0
    t = tmp_path / "target.yaml"
    t.write_text(f"probe: {{serial: {GOOD}}}\ndevice: STM32F407VG\n")
    s = tmp_path / "s.cmm"
    s.write_text("SYStem.Up\nPRINT \"never\"\nENDDO\n")
    assert main(["run", str(s), "--backend", "jlink", "--target", str(t)]) == 3
    err = capsys.readouterr().err
    assert "not powered" in err


def test_run_cli_denied_exit_3_before_script(fake, tmp_path, capsys):
    t = tmp_path / "target.yaml"
    t.write_text(f"probe: {{serial: {KIT}}}\n")
    s = tmp_path / "s.cmm"
    s.write_text("PRINT \"never\"\nENDDO\n")
    assert main(["run", str(s), "--backend", "jlink", "--target", str(t)]) == 3
    out = capsys.readouterr()
    assert "never" not in out.out and "dev kit" in out.err and fake["made"] == 0


def test_run_cli_needs_target(fake, tmp_path):
    s = tmp_path / "s.cmm"
    s.write_text("ENDDO\n")
    assert main(["run", str(s), "--backend", "jlink"]) == 3


def test_leading_zero_serials_are_decimal_not_octal(fake, tmp_path):
    # YAML 1.1 reads 0123 as octal; serials must be read as text
    home = tmp_path / "home"
    home.mkdir()
    (home / "probes.yaml").write_text("deny_serials: [000123456701]\n")
    t = tmp_path / "target.yaml"
    t.write_text("probe: {serial: 000111222333}\ndeny_serials: [000444555666, '000777']\n")
    c = J.load_config(t)
    assert c.serial == GOOD
    assert {123456701, KIT, 777} <= c.deny_serials


@pytest.mark.parametrize("text,match", [
    ("probe: {serial: 1, allow_onboard: maybe}\n", "true or false"),
    ("probe: {serial: 1}\ndeny_serials: 444555666\n", "must be a list"),
    ("probe: {serial: 1}\ndeny_serials: [abc]\n", "not a probe serial"),
    ("probe: {serial: 1}\nspeed: fast\n", "cannot use"),
    ("probe: {serial: 1}\nmin_vtref_mv: lots\n", "whole number"),
    ("probe: [serial\n", "not valid YAML"),
])
def test_bad_config_is_a_target_error(fake, tmp_path, text, match):
    t = tmp_path / "target.yaml"
    t.write_text(text)
    with pytest.raises(TargetError, match=match):
        J.load_config(t)


def test_config_values(fake, tmp_path):
    t = tmp_path / "target.yaml"
    t.write_text("probe: {serial: 1, allow_onboard: yes}\nspeed: 4000\nmin_vtref_mv: 1500\n")
    c = J.load_config(t)
    assert c.allow_onboard is True and c.speed == 4000 and c.min_vtref_mv == 1500


def test_register_map_prefers_whole_names():
    regs = J.register_map([(0, "R13 (SP)"), (1, "R15 (PC)"), (2, "CFBP (CONTROL/FAULTMASK/BASEPRI/PRIMASK)"),
                           (3, "CONTROL"), (4, "XPSR")])
    assert regs["SP"] == regs["R13"] == 0 and regs["PC"] == 1
    assert regs["CONTROL"] == 3 and regs["CFBP"] == 2 and regs["CPSR"] == 4
    assert "FAULTMASK" not in regs                  # only reachable through CFBP


def test_go_and_break_check_the_probe(fake):
    b = J.JLinkBackend(cfg())
    b.connect("up")
    fake["jl"].restart = lambda: False
    with pytest.raises(TargetError, match="could not start"):
        b.go()
    fake["jl"].halted_ = False
    with pytest.raises(TargetError, match="already running"):
        b.go()
    fake["jl"].halt = lambda: False
    with pytest.raises(TargetError, match="did not halt"):
        b.halt()


def test_short_transfers_are_errors(fake):
    b = J.JLinkBackend(cfg())
    b.connect("up")
    fake["jl"].memory_read8 = lambda a, n: [0] * (n - 1)
    with pytest.raises(TargetError, match="got 3 of 4"):
        b.read("D", 0x20000000, 4)
    fake["jl"].memory_write8 = lambda a, d: len(d) - 1
    with pytest.raises(TargetError, match="wrote 1 of 2"):
        b.write("D", 0x20000000, b"\x01\x02")


def test_power_checked_on_every_connect(fake):
    b = J.JLinkBackend(cfg())
    b.connect("up")
    b.disconnect() if hasattr(b, "disconnect") else None
    fake["jl"].vtref = 0
    with pytest.raises(TargetError, match="not powered"):
        b.connect("up")


def test_flash_queue_only_takes_flash_addresses(fake):
    b = J.JLinkBackend(cfg())
    b.connect("up")
    b.flash_add_range(0x08000000, 0x080FFFFF)
    b.flash_begin()
    b.write("D", 0x20000000, b"\x11")             # RAM: written now (e.g. a flash algorithm's buffer)
    b.write("P", 0x08000000, b"\xAA")
    assert fake["jl"].mem == {0x20000000: 0x11}
    assert b.read("P", 0x08000000, 1) == b"\xAA"   # queued data is visible before programming
    b.flash_end()
    assert [c for c in fake["jl"].calls if c[0] == "flash"] == [("flash", 0x08000000, b"\xAA")]


def test_bad_jlink_dll_is_a_target_error(monkeypatch):
    import pylink

    def boom(*a, **k):
        raise TypeError("Expected to be given a valid DLL.")
    monkeypatch.setattr(pylink, "JLink", boom)
    with pytest.raises(TargetError, match="J-Link Software Pack"):
        J._make_jlink(cfg())


@pytest.mark.parametrize("kind,text,match", [
    ("ihex", ":0400000001020304F2\n:02000000\n", "length"),
    ("ihex", ":04000000010203\n", "length"),
    ("srecord", "S1050000AA\n", "length"),
    ("ihex", ":zz\n", "not a valid"),
    ("elf", "\x7fELF", "not an ELF"),
])
def test_bad_images_are_script_errors(tmp_path, kind, text, match):
    from cmmrun.backends.images import load_image
    from cmmrun.interp.values import PracticeError
    f = tmp_path / "img"
    f.write_bytes(text.encode("latin-1"))
    with pytest.raises(PracticeError, match=match):
        load_image(f, kind)
