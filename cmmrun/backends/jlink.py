"""SEGGER J-Link backend (through pylink-square and the J-Link DLL).

Safety rules come first, because other SEGGER probes (for example the J-Link
built into a dev kit) may be plugged into the same PC:

1. The probe is chosen by serial number from target.yaml. Nothing ever lets
   the J-Link software pick a probe.
2. `deny_serials` (in target.yaml and in ~/.cmmrun/probes.yaml) lists probes
   that must never be opened. A denied serial is refused before the DLL is
   even loaded.
3. Probes are listed through the operating system's USB device list, which
   does not open them. The chosen serial must be present there.
4. A probe that shows up as a USB composite device with serial ports, which
   is how J-Links built into dev kits appear, is refused unless
   `probe.allow_onboard: true`. After opening, the product name is checked
   again for " OB" (on-board).
5. Automatic probe firmware updates are disabled before the probe is opened.
   pylink's `disable_dialog_boxes()` is deliberately not used: it sends
   `SilentUpdateFW`, which updates firmware without asking.
6. Unsecuring a locked chip (which mass-erases it) is always declined.
7. A target with no power (VTref below `min_vtref_mv`) is a clean target
   error (exit code 3) before any connect is attempted.
"""
from __future__ import annotations

import functools
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field

from ..interp.values import TargetError
from .base import Backend

SEGGER_VID = 0x1366

# Sent before open(). DisableAutoUpdateFW is mandatory; the rest only hide dialogs.
PRE_OPEN_REQUIRED = ["DisableAutoUpdateFW"]
PRE_OPEN_QUIET = ["SuppressInfoUpdateFW", "SetBatchMode = 1", "HideDeviceSelection = 1",
                  "SuppressControlPanel", "DisableInfoWinFlashDL", "DisableInfoWinFlashBPs"]
NEVER_SEND = {"SilentUpdateFW", "UpdateFW", "InvalidateFW"}

REG_ALIASES = {"CPSR": "XPSR", "R13": "SP", "R14": "LR", "R15": "PC"}


# --------------------------------------------------------------------------- config

@dataclass
class JLinkConfig:
    serial: int | None = None
    allow_onboard: bool = False
    deny_serials: set[int] = field(default_factory=set)
    device: str | None = None                  # J-Link device name, e.g. STM32F407VG
    cpu_map: dict[str, str] = field(default_factory=dict)   # SYStem.CPU name -> J-Link device
    interface: str = "swd"                     # swd | jtag
    speed: int | str = "auto"                  # kHz, "auto" or "adaptive"
    min_vtref_mv: int = 1000
    dll: str | None = None
    source: str = ""                           # where the config came from, for messages

    def device_for(self, cpu: str) -> str:
        for k, v in self.cpu_map.items():
            if k.upper() == cpu.upper():
                return v
        if self.device:
            return self.device
        if cpu:
            return cpu
        raise TargetError(f"no J-Link device: set `device:` in {self.source or 'target.yaml'} or use SYStem.CPU")


def _serial(v, where: str) -> int:
    try:
        return int(str(v).strip(), 10)
    except ValueError:
        raise TargetError(f"{where}: {v!r} is not a probe serial number") from None


def user_deny_file(home: str | os.PathLike | None = None) -> pathlib.Path:
    base = pathlib.Path(home) if home else pathlib.Path(os.environ.get("CMMRUN_HOME") or pathlib.Path.home() / ".cmmrun")
    return base / "probes.yaml"


def _load_yaml(path: pathlib.Path) -> dict:
    import yaml
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise TargetError(f"{path}: expected a mapping at the top level")
    return data


def user_deny_serials(home=None) -> set[int]:
    p = user_deny_file(home)
    if not p.exists():
        return set()
    return {_serial(s, str(p)) for s in (_load_yaml(p).get("deny_serials") or [])}


def load_config(path: str | os.PathLike | None, home=None) -> JLinkConfig:
    if not path:
        raise TargetError("the J-Link backend needs --target target.yaml naming the probe serial "
                          "(see examples/target.example.yaml)")
    p = pathlib.Path(path)
    if not p.exists():
        raise TargetError(f"target file not found: {p}")
    raw = _load_yaml(p)
    probe = raw.get("probe") or {}
    if not isinstance(probe, dict):
        raise TargetError(f"{p}: `probe:` must be a mapping with `serial:`")
    cfg = JLinkConfig(source=str(p))
    if probe.get("serial") is not None:
        cfg.serial = _serial(probe["serial"], f"{p}: probe.serial")
    cfg.allow_onboard = bool(probe.get("allow_onboard", False))
    cfg.deny_serials = {_serial(s, f"{p}: deny_serials") for s in (raw.get("deny_serials") or [])}
    cfg.deny_serials |= user_deny_serials(home)
    cfg.device = raw.get("device")
    cfg.cpu_map = {str(k): str(v) for k, v in (raw.get("cpu_map") or {}).items()}
    cfg.interface = str(raw.get("interface", "swd")).lower()
    if cfg.interface not in ("swd", "jtag"):
        raise TargetError(f"{p}: interface must be swd or jtag, not {cfg.interface!r}")
    sp = raw.get("speed", "auto")
    cfg.speed = sp if sp in ("auto", "adaptive") else int(sp)
    cfg.min_vtref_mv = int(raw.get("min_vtref_mv", 1000))
    cfg.dll = raw.get("dll")
    return cfg


# --------------------------------------------------------------------------- probe listing

@dataclass(frozen=True)
class Probe:
    serial: int
    pid: int
    name: str
    composite: bool     # USB composite device with extra interfaces (serial ports): typical of on-board J-Links


_WIN_TOP = re.compile(r"^USB\\VID_1366&PID_([0-9A-F]{4})\\(\d+)$", re.I)
_WIN_CHILD = re.compile(r"^USB\\VID_1366&PID_([0-9A-F]{4})&MI_", re.I)


def parse_pnp(lines: list[str]) -> list[Probe]:
    """Lines of `InstanceId<TAB>Class<TAB>FriendlyName` from Get-PnpDevice."""
    rows = [(ln.split("\t") + ["", ""])[:3] for ln in lines if ln.strip()]
    child_pids = {int(m.group(1), 16) for iid, _, _ in rows if (m := _WIN_CHILD.match(iid.strip()))}
    out = []
    for iid, cls, name in rows:
        m = _WIN_TOP.match(iid.strip())
        if not m:
            continue
        pid = int(m.group(1), 16)
        composite = "composite" in name.lower() or (cls.strip().upper() == "USB" and pid in child_pids)
        out.append(Probe(int(m.group(2)), pid, name.strip(), composite))
    return sorted(out, key=lambda p: p.serial)


def _list_windows() -> list[Probe]:
    ps = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\\VID_1366&*' } | "
          "ForEach-Object { \"$($_.InstanceId)`t$($_.Class)`t$($_.FriendlyName)\" }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TargetError(f"cannot list USB devices: {e}") from None
    if r.returncode != 0:
        raise TargetError(f"cannot list USB devices: {r.stderr.strip()[:300]}")
    return parse_pnp(r.stdout.splitlines())


def _list_sysfs(root="/sys/bus/usb/devices") -> list[Probe]:
    out = []
    for d in pathlib.Path(root).glob("*"):
        try:
            if int((d / "idVendor").read_text().strip(), 16) != SEGGER_VID:
                continue
            pid = int((d / "idProduct").read_text().strip(), 16)
            serial = int((d / "serial").read_text().strip())
            nif = int((d / "bNumInterfaces").read_text().strip() or 1)
            name = (d / "product").read_text().strip() if (d / "product").exists() else ""
        except (OSError, ValueError):
            continue
        out.append(Probe(serial, pid, name, nif > 1))
    return sorted(out, key=lambda p: p.serial)


def list_probes() -> list[Probe]:
    """SEGGER probes from the OS USB device list. Never opens a probe."""
    if os.name == "nt":
        return _list_windows()
    if sys.platform.startswith("linux"):
        return _list_sysfs()
    raise TargetError("listing USB probes is not supported on this OS yet")


def select_probe(cfg: JLinkConfig, probes: list[Probe]) -> Probe:
    def present():
        return ", ".join(f"{p.serial}{' (denied)' if p.serial in cfg.deny_serials else ''}"
                         f"{' (on-board?)' if p.composite else ''}" for p in probes) or "none"
    if cfg.serial is None:
        raise TargetError(f"{cfg.source or 'target.yaml'} must name the probe (probe: serial: ...). "
                          f"SEGGER probes present: {present()}")
    if cfg.serial in cfg.deny_serials:
        raise TargetError(f"refusing probe {cfg.serial}: it is on the deny list")
    hit = [p for p in probes if p.serial == cfg.serial]
    if not hit:
        raise TargetError(f"probe {cfg.serial} is not connected. SEGGER probes present: {present()}")
    p = hit[0]
    if p.composite and not cfg.allow_onboard:
        raise TargetError(f"refusing probe {cfg.serial}: it looks like a J-Link built into a dev kit "
                          "(USB composite device with serial ports). Set probe.allow_onboard: true "
                          "in target.yaml only if this really is the probe to use")
    return p


# --------------------------------------------------------------------------- backend

def _make_jlink(cfg: JLinkConfig):
    """Create a pylink.JLink that will never unsecure (mass-erase) a chip. Separate for tests."""
    import pylink
    from pylink import enums

    def never_unsecure(title, msg, flags):
        return enums.JLinkFlags.DLG_BUTTON_NO

    lib = pylink.Library(cfg.dll) if cfg.dll else None
    kw = {"unsecure_hook": never_unsecure}
    return (pylink.JLink(lib=lib, **kw) if lib else pylink.JLink(**kw)), pylink.errors.JLinkException


def _target_errors(fn):
    @functools.wraps(fn)
    def wrap(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except TargetError:
            raise
        except self._exc as e:
            raise TargetError(f"J-Link: {e}") from None
    return wrap


class JLinkBackend(Backend):
    name = "jlink"

    def __init__(self, cfg: JLinkConfig):
        super().__init__()
        self.cfg = cfg
        self.jl = None
        self._exc: tuple = (OSError,)
        self._connected = False
        self._regs: dict[str, int] = {}
        self._bp = None
        self._flash: list[tuple[int, bytes]] | None = None
        self.probe: Probe | None = None

    def check(self) -> Probe:
        """Validate config and probe choice without opening anything."""
        self.probe = select_probe(self.cfg, list_probes())
        return self.probe

    # ---- session
    def _exec(self, cmd: str):
        if cmd.split("=")[0].strip() in NEVER_SEND:
            raise TargetError(f"internal: refusing to send {cmd!r} to the J-Link DLL")
        return self.jl.exec_command(cmd)

    def _open(self):
        probe = self.check()                          # re-check right before opening
        self.jl, exc = _make_jlink(self.cfg)
        self._exc = (exc,)
        try:
            for c in PRE_OPEN_REQUIRED:
                self._exec(c)
        except exc as e:
            self.jl = None
            raise TargetError(f"J-Link DLL refused {PRE_OPEN_REQUIRED}: {e}; not opening the probe") from None
        for c in PRE_OPEN_QUIET:
            try:
                self._exec(c)
            except exc:
                pass
        try:
            self.jl.open(serial_no=probe.serial)
        except exc as e:
            self.jl = None
            raise TargetError(f"cannot open probe {probe.serial}: {e}") from None
        try:
            if int(self.jl.serial_number) != probe.serial:
                raise TargetError(f"opened probe reports serial {self.jl.serial_number}, expected {probe.serial}")
            product = str(self.jl.product_name)
            if re.search(r"\bOB\b", product) and not self.cfg.allow_onboard:
                raise TargetError(f"refusing probe {probe.serial}: product {product!r} is an on-board J-Link")
            mv = int(self.jl.hardware_status.voltage)
            self.log("probe", product=product, firmware=str(self.jl.firmware_version), vtref_mv=mv)
            if mv < self.cfg.min_vtref_mv:
                raise TargetError(f"target is not powered: VTref is {mv} mV (need at least "
                                  f"{self.cfg.min_vtref_mv} mV). Check the board's power and the VTref pin")
        except BaseException:
            self._close_probe()
            raise

    def _close_probe(self):
        if self.jl is not None:
            try:
                self.jl.close()
            except self._exc:
                pass
        self.jl = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @_target_errors
    def connect(self, mode: str):
        if self.jl is None:
            self._open()
        from pylink import enums
        tif = enums.JLinkInterfaces.SWD if self.cfg.interface == "swd" else enums.JLinkInterfaces.JTAG
        if not self.jl.set_tif(tif):
            raise TargetError(f"probe cannot use interface {self.cfg.interface}")
        speed = self.options.get("clock", self.cfg.speed)
        device = self.cfg.device_for(self.cpu)
        self.jl.connect(device, speed=_khz(speed))
        self._regs = {}
        for idx in self.jl.register_list():
            for n in re.findall(r"[A-Za-z][A-Za-z0-9_]*", self.jl.register_name(idx)):
                self._regs.setdefault(n.upper(), idx)
        self._connected = True
        self.log("connect", mode=mode, device=device, interface=self.cfg.interface)

    def disconnect(self):
        self._close_probe()
        self.log("disconnect")

    def _need(self):
        if not self._connected:
            raise TargetError("target is not connected (SYStem.Up first)")

    # ---- execution
    @_target_errors
    def reset(self, halt: bool = True):
        self._need()
        self.jl.reset(halt=halt)
        self.log("reset", halt=halt)

    @_target_errors
    def go(self, until: int | None = None):
        self._need()
        self._clear_bp()
        if until is not None:
            self._bp = self.jl.breakpoint_set(until, thumb=True)
        self.jl.restart()
        self.log("go", until=until)

    def _clear_bp(self):
        if self._bp is not None:
            self.jl.breakpoint_clear(self._bp)
            self._bp = None

    @_target_errors
    def halt(self):
        self._need()
        self.jl.halt()
        self._clear_bp()
        self.log("halt")

    @_target_errors
    def step(self, n: int = 1):
        self._need()
        for _ in range(n):
            self.jl.step()
        self.log("step", n=n)

    @_target_errors
    def is_running(self) -> bool:
        self._need()
        running = not self.jl.halted()
        if not running:
            self._clear_bp()
        return running

    # ---- memory and registers
    @_target_errors
    def read(self, access: str, addr: int, size: int) -> bytes:
        self._need()
        return bytes(self.jl.memory_read8(addr, size))

    @_target_errors
    def write(self, access: str, addr: int, data: bytes):
        self._need()
        if self._flash is not None:
            self._flash.append((addr, bytes(data)))
        else:
            self.jl.memory_write8(addr, list(data))
        self.log("write", access=access, addr=addr, size=len(data))

    def _reg(self, name: str) -> int:
        n = name.upper()
        for key in (n, REG_ALIASES.get(n, n)):
            if key in self._regs:
                return self._regs[key]
        raise TargetError(f"unknown register {name!r}")

    @_target_errors
    def read_reg(self, name: str) -> int:
        self._need()
        return int(self.jl.register_read(self._reg(name))) & 0xFFFFFFFF

    @_target_errors
    def write_reg(self, name: str, value: int):
        self._need()
        self.jl.register_write(self._reg(name), value & 0xFFFFFFFF)
        self.log("write_reg", reg=name.upper(), value=value & 0xFFFFFFFF)

    # ---- flash: collect writes, program them with the J-Link's own flash loader
    def flash_begin(self):
        self._flash = []
        super().flash_begin()

    @_target_errors
    def flash_end(self):
        pending, self._flash = self._flash or [], None
        for addr, data in merge_segments(pending):
            self._need()
            self.jl.flash(data, addr)
            self.log("flash", addr=addr, size=len(data))
        super().flash_end()

    def close(self):
        self._close_probe()


def _khz(speed):
    if isinstance(speed, str):
        s = speed.strip().lower()
        if s in ("auto", "adaptive"):
            return s
        m = re.fullmatch(r"(\d+\.?\d*)\s*(mhz|khz|hz)?", s)
        if not m:
            raise TargetError(f"cannot use JTAG clock {speed!r}")
        v, unit = float(m.group(1)), m.group(2) or "khz"
        return max(5, int(v * {"mhz": 1000, "khz": 1, "hz": 0.001}[unit]))
    return int(speed)


def merge_segments(writes: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    """Merge writes into contiguous segments; later writes win where they overlap."""
    mem: dict[int, int] = {}
    for addr, data in writes:
        for i, b in enumerate(data):
            mem[addr + i] = b
    out: list[tuple[int, bytearray]] = []
    for a in sorted(mem):
        if out and out[-1][0] + len(out[-1][1]) == a:
            out[-1][1].append(mem[a])
        else:
            out.append((a, bytearray([mem[a]])))
    return [(a, bytes(b)) for a, b in out]
