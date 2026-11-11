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
import time
from dataclasses import dataclass, field

from ..interp.values import TargetError
from .base import Backend

SEGGER_VID = 0x1366

# Sent before open(). DisableAutoUpdateFW is mandatory; the rest only hide dialogs.
PRE_OPEN_REQUIRED = ["DisableAutoUpdateFW"]
PRE_OPEN_QUIET = ["SuppressInfoUpdateFW", "SetBatchMode = 1", "HideDeviceSelection = 1",
                  "SuppressControlPanel", "DisableInfoWinFlashDL", "DisableInfoWinFlashBPs"]
NEVER_SEND = {"SilentUpdateFW", "UpdateFW", "InvalidateFW"}

REG_ALIASES = {"CPSR": "XPSR", "R13": "SP", "R14": "LR", "R15": "PC",
               "SP": "R13", "LR": "R14", "PC": "R15", "XPSR": "CPSR"}
MIN_KHZ, MAX_KHZ = 5, 50000


# --------------------------------------------------------------------------- config

@dataclass
class JLinkConfig:
    serial: int | None = None
    allow_onboard: bool = False
    deny_serials: set[int] = field(default_factory=set)
    device: str | None = None                  # J-Link device name, e.g. STM32F407VG
    cpu_map: dict[str, str] = field(default_factory=dict)   # SYStem.CPU name -> J-Link device
    interface: str = "swd"                     # swd | jtag
    speed: int | str = "auto"                  # kHz (or "4MHz"), "auto" or "adaptive"
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
    """Serials are read as text and parsed in base 10, so `000123456789` is 123456789 (YAML 1.1
    would have read it as an octal number and the deny list would silently not match)."""
    s = str(v).strip()
    if not s.isdigit():
        raise TargetError(f"{where}: {v!r} is not a probe serial number")
    return int(s, 10)


def _serials(v, where: str) -> set[int]:
    if v in (None, ""):
        return set()
    if not isinstance(v, list):
        raise TargetError(f"{where}: must be a list, e.g. deny_serials: [\"123456789\"]")
    return {_serial(s, where) for s in v}


def _bool(v, where: str) -> bool:
    s = str(v).strip().lower()
    if s in ("true", "yes", "on"):
        return True
    if s in ("false", "no", "off", ""):
        return False
    raise TargetError(f"{where}: expected true or false, not {v!r}")


def _int(v, where: str) -> int:
    try:
        return int(str(v).strip(), 10)
    except ValueError:
        raise TargetError(f"{where}: {v!r} is not a whole number") from None


def user_deny_file(home: str | os.PathLike | None = None) -> pathlib.Path:
    base = pathlib.Path(home) if home else pathlib.Path(os.environ.get("CMMRUN_HOME") or pathlib.Path.home() / ".cmmrun")
    return base / "probes.yaml"


def _load_yaml(path: pathlib.Path) -> dict:
    import yaml
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.load(fh, Loader=yaml.BaseLoader) or {}     # every value stays a string
    except OSError as e:
        raise TargetError(f"cannot read {path}: {e.strerror or e}") from None
    except yaml.YAMLError as e:
        raise TargetError(f"{path} is not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise TargetError(f"{path}: expected a mapping at the top level")
    return data


def user_deny_serials(home=None) -> set[int]:
    p = user_deny_file(home)
    if not p.exists():
        return set()
    return _serials(_load_yaml(p).get("deny_serials"), f"{p}: deny_serials")


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
    cfg.allow_onboard = _bool(probe.get("allow_onboard", "false"), f"{p}: probe.allow_onboard")
    cfg.deny_serials = _serials(raw.get("deny_serials"), f"{p}: deny_serials")
    cfg.deny_serials |= user_deny_serials(home)
    cfg.device = raw.get("device") or None
    cmap = raw.get("cpu_map") or {}
    if not isinstance(cmap, dict):
        raise TargetError(f"{p}: cpu_map must be a mapping of SYStem.CPU name to J-Link device")
    cfg.cpu_map = {str(k): str(v) for k, v in cmap.items()}
    cfg.interface = str(raw.get("interface", "swd")).lower()
    if cfg.interface not in ("swd", "jtag"):
        raise TargetError(f"{p}: interface must be swd or jtag, not {cfg.interface!r}")
    cfg.speed = _khz(raw.get("speed", "auto"), default_unit="khz", where=f"{p}: speed")
    cfg.min_vtref_mv = _int(raw.get("min_vtref_mv", "1000"), f"{p}: min_vtref_mv")
    cfg.dll = raw.get("dll") or None
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
    ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
          "Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\\VID_1366&*' } | "
          "ForEach-Object { \"$($_.InstanceId)`t$($_.Class)`t$($_.FriendlyName)\" }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
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

    kw = {"unsecure_hook": never_unsecure}
    try:
        lib = pylink.Library(cfg.dll) if cfg.dll else None
        jl = pylink.JLink(lib=lib, **kw) if lib else pylink.JLink(**kw)
    except (OSError, TypeError) as e:
        # pylink raises TypeError("Expected to be given a valid DLL.") when nothing is installed
        raise TargetError(f"cannot load the J-Link DLL ({e}). Install the J-Link Software Pack from "
                          "SEGGER, or set `dll:` in target.yaml") from None
    return jl, pylink.errors.JLinkException


def _target_errors(fn):
    @functools.wraps(fn)
    def wrap(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except TargetError:
            raise
        except self._exc as e:
            raise TargetError(f"J-Link: {e}") from None
        except ValueError as e:         # pylink checks some arguments itself (speed, sizes)
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
        self._dev_flash: list[tuple[int, int]] = []        # (lo, hi) from the J-Link device info
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

    def _open(self, check_power: bool = True):
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
            self.log("probe", product=product, firmware=str(self.jl.firmware_version))
            if check_power:
                self._check_power()
        except BaseException:
            self._close_probe()
            raise

    def _check_power(self):
        mv = int(self.jl.hardware_status.voltage)
        self.log("vtref", mv=mv)
        if mv < self.cfg.min_vtref_mv:
            raise TargetError(f"target is not powered: VTref is {mv} mV (need at least "
                              f"{self.cfg.min_vtref_mv} mV). Check the board's power and the VTref pin")

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
        else:
            self._check_power()             # e.g. SYStem.Up retried from an ON ERROR handler
        from pylink import enums
        iface = self.options.get("interface", self.cfg.interface)     # SYStem.CONFIG.DEBUGPORTTYPE wins
        tif = enums.JLinkInterfaces.SWD if iface == "swd" else enums.JLinkInterfaces.JTAG
        if not self.jl.set_tif(tif):
            raise TargetError(f"probe cannot use interface {iface}")
        clock = self.options.get("clock")
        speed = self.cfg.speed if clock is None else _khz(clock, where="SYStem.JtagClock")
        device = self.cfg.device_for(self.cpu)
        self.jl.connect(device, speed=speed)
        self._regs = register_map((idx, self.jl.register_name(idx)) for idx in self.jl.register_list())
        self._dev_flash = _device_flash(getattr(self.jl, "_device", None))
        self._connected = True
        self.log("connect", mode=mode, device=device, interface=iface)

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
        if not self.jl.halted():
            raise TargetError("Go: the core is already running")
        if until is not None:
            self._bp = self.jl.breakpoint_set(until, thumb=True)
        if self.jl.restart() is False:
            self._clear_bp()
            raise TargetError("Go: the J-Link could not start the core")
        self.log("go", until=until)

    def _clear_bp(self):
        if self._bp is not None:
            self.jl.breakpoint_clear(self._bp)
            self._bp = None

    @_target_errors
    def halt(self):
        self._need()
        self._halt()
        self._clear_bp()
        self.log("halt")

    def _halt(self):
        # pylink's halt() sleeps a fixed second after success; call the DLL and poll instead
        dll = getattr(self.jl, "_dll", None)
        if dll is None:
            ok = self.jl.halt() is not False
        else:
            ok = int(dll.JLINKARM_Halt()) == 0
        if ok:
            for _ in range(100):
                if self.jl.halted():
                    return
                time.sleep(0.01)
        raise TargetError("Break: the core did not halt")

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
        data = bytes(self.jl.memory_read8(addr, size))
        if len(data) != size:
            raise TargetError(f"read at 0x{addr:X}: got {len(data)} of {size} bytes")
        if self._flash:
            buf = bytearray(data)       # show writes queued for flash programming
            for a, d in self._flash:
                for i, b in enumerate(d):
                    if addr <= a + i < addr + size:
                        buf[a + i - addr] = b
            data = bytes(buf)
        return data

    @_target_errors
    def write(self, access: str, addr: int, data: bytes):
        self._need()
        if self._flash is not None and self._is_flash(addr, len(data)):
            self._flash.append((addr, bytes(data)))
            self.log("write", access=access, addr=addr, size=len(data), queued=True)
            return
        n = self.jl.memory_write8(addr, list(data))
        if n is not None and n != len(data):
            raise TargetError(f"write at 0x{addr:X}: wrote {n} of {len(data)} bytes")
        self.log("write", access=access, addr=addr, size=len(data))

    def _is_flash(self, addr: int, size: int) -> bool:
        ranges = self.flash_ranges or self._dev_flash
        if not ranges:
            return True                 # nothing known: let the J-Link flash loader sort it out
        return any(lo <= addr + size - 1 and addr <= hi for lo, hi in ranges)

    def _reg(self, name: str) -> int:
        n = name.upper()
        if n in self._regs:
            return self._regs[n]
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
        segs = merge_segments(pending)
        if segs:
            self._need()
            self._download(segs)
        super().flash_end()

    @_target_errors
    def flash_erase(self, lo: int | None = None, hi: int | None = None):
        self._need()
        if lo is None:
            dll = getattr(self.jl, "_dll", None)
            self._halt_quiet()
            res = self.jl.erase() if dll is None else int(dll.JLINK_EraseChip())
            if res is not None and res < 0:
                raise TargetError(f"FLASH.Erase: chip erase failed ({res})")
            self.log("flash_erase", all=True)
        else:
            # erased flash reads as 0xFF: program it so through the flash loader
            self._download([(lo, b"\xff" * (hi - lo + 1))])
            self.log("flash_erase", lo=lo, hi=hi)

    def _halt_quiet(self):
        try:
            if not self.jl.halted():
                self._halt()
        except (TargetError, *self._exc):
            pass

    def _download(self, segs: list[tuple[int, bytes]]):
        """One flash-loader session for all segments: a sector is erased and programmed once,
        and the core is halted once (pylink's flash() does both per call)."""
        dll = getattr(self.jl, "_dll", None)
        if dll is None:                 # test doubles without a DLL
            for addr, data in segs:
                self.jl.flash(data, addr)
                self.log("flash", addr=addr, size=len(data))
            return
        self._halt_quiet()
        dll.JLINK_SetFlashProgProgressCallback(0)
        dll.JLINKARM_BeginDownload(0)
        for addr, data in segs:
            n = int(dll.JLINKARM_WriteMem(addr, len(data), data))
            if n < 0:
                dll.JLINKARM_EndDownload()
                raise TargetError(f"flash download at 0x{addr:X} failed ({n})")
            self.log("flash", addr=addr, size=len(data))
        res = int(dll.JLINKARM_EndDownload())
        if res < 0:
            raise TargetError(f"flash programming failed (J-Link error {res})")

    def close(self):
        self._close_probe()


def _khz(speed, default_unit: str = "hz", where: str = "JTAG clock"):
    """A J-Link speed in kHz, or "auto"/"adaptive". TRACE32 `SYStem.JtagClock` values without a
    unit are in Hz (`1000000.` is 1 MHz); target.yaml `speed:` without a unit is in kHz."""
    if isinstance(speed, int) and not isinstance(speed, bool):
        s = str(speed)
    else:
        s = str(speed).strip().lower()
    s = re.sub(r"^c?tck\s+", "", s)                     # CTCK 10MHz: a fixed clock
    if s in ("auto", "adaptive"):
        return s
    if s in ("rtck", "artck") or s.startswith(("rtck ", "artck ")):
        return "adaptive"
    m = re.fullmatch(r"(\d+(?:\.\d*)?)\s*(mhz|khz|hz|m|k)?", s)
    if not m:
        raise TargetError(f"{where}: cannot use {speed!r}")
    unit = {"m": "mhz", "k": "khz"}.get(m.group(2), m.group(2)) or default_unit
    khz = float(m.group(1)) * {"mhz": 1000, "khz": 1, "hz": 0.001}[unit]
    if not MIN_KHZ <= khz <= MAX_KHZ:
        raise TargetError(f"{where}: {speed!r} is outside what a J-Link can do "
                          f"({MIN_KHZ} kHz to {MAX_KHZ // 1000} MHz)")
    return int(khz)


def register_map(names) -> dict[str, int]:
    """Register name -> index from the DLL's names such as "R13 (SP)" or
    "CFBP (CONTROL/FAULTMASK/BASEPRI/PRIMASK)". The first word of a name always wins over a word
    in parentheses, so a combined register never hides the separate ones."""
    names = list(names)
    regs: dict[str, int] = {}
    for idx, name in names:
        words = re.findall(r"[A-Za-z][A-Za-z0-9_]*", str(name))
        if words:
            regs.setdefault(words[0].upper(), idx)
    for idx, name in names:
        m = re.search(r"\(([A-Za-z][A-Za-z0-9_]*)\)", str(name))
        if m:                                           # a single alias: "R13 (SP)"
            regs.setdefault(m.group(1).upper(), idx)
    for a, b in REG_ALIASES.items():
        if a not in regs and b in regs:
            regs[a] = regs[b]
    return regs


def _device_flash(dev) -> list[tuple[int, int]]:
    """Flash areas from pylink's JLinkDeviceInfo, if it has them."""
    out = []
    try:
        for area in getattr(dev, "aFlashArea", []) or []:
            if int(area.Size):
                out.append((int(area.Addr), int(area.Addr) + int(area.Size) - 1))
        if not out and int(getattr(dev, "FlashSize", 0) or 0):
            out.append((int(dev.FlashAddr), int(dev.FlashAddr) + int(dev.FlashSize) - 1))
    except (TypeError, ValueError, AttributeError):
        return []
    return out


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
