"""A simulated target: sparse memory, registers, run state and a virtual clock.

Nothing here touches hardware. It is what `run --dry-run` uses, and what the
tests use. After `Go`, the mock target "runs" for `halt_after` seconds of
virtual time, then stops as if it hit a breakpoint, so scripts that do
`Go` + `WAIT !STATE.RUN() 1.s` behave sensibly.
"""
from __future__ import annotations

from ..interp.values import TargetError
from .base import Backend

CORTEX_M_REGS = [f"R{i}" for i in range(13)] + ["SP", "LR", "PC", "XPSR", "MSP", "PSP", "PRIMASK",
                                                 "BASEPRI", "FAULTMASK", "CONTROL"]
ALIASES = {"R13": "SP", "R14": "LR", "R15": "PC", "CPSR": "XPSR"}


class MockBackend(Backend):
    name = "mock"

    def __init__(self, halt_after: float = 0.01, require_connect: bool = True):
        super().__init__()
        self.mem: dict[int, int] = {}
        self.regs: dict[str, int] = {r: 0 for r in CORTEX_M_REGS}
        self._connected = False
        self._running_until: float | None = None
        self._clock = 0.0
        self.halt_after = halt_after
        self.require_connect = require_connect
        self.flash_open = False

    # ---- virtual clock
    def now(self) -> float:
        return self._clock

    def sleep(self, seconds: float):
        self._clock += max(0.0, seconds)

    # ---- session
    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self, mode: str):
        self._connected = True
        self._running_until = None if mode != "attach" else self._running_until
        self.log("connect", mode=mode)

    def disconnect(self):
        self._connected = False
        self.log("disconnect")

    def _need(self):
        if self.require_connect and not self._connected:
            raise TargetError("target is not connected (SYStem.Up first)")

    # ---- execution
    def reset(self, halt: bool = True):
        self._need()
        self.regs = {r: 0 for r in CORTEX_M_REGS}
        sp, pc = self._u32(0), self._u32(4)
        self.regs["SP"] = self.regs["MSP"] = sp
        self.regs["PC"] = pc & ~1
        self.regs["XPSR"] = 0x01000000
        self._running_until = None if halt else float("inf")
        self.log("reset", halt=halt)

    def go(self, until: int | None = None):
        self._need()
        self._running_until = self._clock + self.halt_after
        self.log("go", until=until)
        if until is not None:
            self._pending_pc = until

    def halt(self):
        self._need()
        self._running_until = None
        self.log("halt")

    def step(self, n: int = 1):
        self._need()
        if self.is_running():
            raise TargetError("cannot step while running")
        self.regs["PC"] = (self.regs["PC"] + 2 * n) & 0xFFFFFFFF
        self.log("step", n=n)

    def is_running(self) -> bool:
        if self._running_until is not None and self._clock >= self._running_until:
            self._running_until = None
            if getattr(self, "_pending_pc", None) is not None:
                self.regs["PC"] = self._pending_pc
                self._pending_pc = None
        return self._running_until is not None

    # ---- memory and registers
    def _u32(self, addr: int) -> int:
        return int.from_bytes(bytes(self.mem.get(addr + i, 0) for i in range(4)), "little")

    def read(self, access: str, addr: int, size: int) -> bytes:
        self._need()
        return bytes(self.mem.get(addr + i, 0) for i in range(size))

    def write(self, access: str, addr: int, data: bytes):
        self._need()
        for i, b in enumerate(data):
            self.mem[addr + i] = b
        self.log("write", access=access, addr=addr, size=len(data))

    def _reg(self, name: str) -> str:
        n = name.upper()
        n = ALIASES.get(n, n)
        if n not in self.regs:
            raise TargetError(f"unknown register {name!r}")
        return n

    def read_reg(self, name: str) -> int:
        self._need()
        return self.regs[self._reg(name)]

    def write_reg(self, name: str, value: int):
        self._need()
        r = self._reg(name)
        self.regs[r] = value & 0xFFFFFFFF
        if r in ("SP", "MSP"):
            self.regs["SP"] = self.regs["MSP"] = value & 0xFFFFFFFF
        self.log("write_reg", reg=r, value=value & 0xFFFFFFFF)

    def flash_begin(self):
        self.flash_open = True
        super().flash_begin()

    def flash_end(self):
        self.flash_open = False
        super().flash_end()
