"""The interface every backend implements.

The interpreter only talks to a target through these methods. Addresses are
plain integers; the access class (`SD:`, `P:`, ...) is passed along but most
backends map every class to the same physical memory.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field


@dataclass
class Event:
    """One action against the target, for the run log."""
    t: float
    op: str
    detail: dict = field(default_factory=dict)


class Backend(abc.ABC):
    name = "base"

    def __init__(self):
        self.events: list[Event] = []
        self.cpu = ""
        self.options: dict[str, str] = {}
        self.flash_ranges: list[tuple[int, int]] = []    # from FLASH.Create

    def log(self, op: str, **detail):
        self.events.append(Event(round(self.now(), 6), op, detail))

    # ---- clock (the mock backend uses virtual time)
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float):
        time.sleep(max(0.0, seconds))

    # ---- session
    def set_cpu(self, name: str):
        self.cpu = name
        self.log("cpu", name=name)

    def set_option(self, key: str, value: str):
        self.options[key] = value
        self.log("option", key=key, value=value)

    @abc.abstractmethod
    def connect(self, mode: str): ...            # "up" | "attach" | "prepare"

    @abc.abstractmethod
    def disconnect(self): ...

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    # ---- execution
    @abc.abstractmethod
    def reset(self, halt: bool = True): ...

    @abc.abstractmethod
    def go(self, until: int | None = None): ...

    @abc.abstractmethod
    def halt(self): ...

    @abc.abstractmethod
    def step(self, n: int = 1): ...

    @abc.abstractmethod
    def is_running(self) -> bool: ...

    # ---- memory and registers
    @abc.abstractmethod
    def read(self, access: str, addr: int, size: int) -> bytes: ...

    @abc.abstractmethod
    def write(self, access: str, addr: int, data: bytes): ...

    @abc.abstractmethod
    def read_reg(self, name: str) -> int: ...

    @abc.abstractmethod
    def write_reg(self, name: str, value: int): ...

    # ---- flash: between flash_begin and flash_end, writes to flash ranges are
    # collected and programmed at flash_end (FLASH.ReProgram ALL ... OFF).
    def flash_begin(self):
        self.log("flash_begin")

    def flash_end(self):
        self.log("flash_end")

    def flash_add_range(self, lo: int, hi: int):
        self.flash_ranges.append((lo, hi))
        self.log("flash_range", lo=lo, hi=hi)

    def flash_clear_ranges(self):
        self.flash_ranges = []

    def flash_erase(self, lo: int | None = None, hi: int | None = None):
        """Erase the whole flash (lo is None) or a range. Erased flash reads as 0xFF."""
        raise NotImplementedError

    def close(self):
        if self.connected:
            self.disconnect()
