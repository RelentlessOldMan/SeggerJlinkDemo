"""Read program images into (address, bytes) segments: ELF, Intel HEX, S-record, raw binary."""
from __future__ import annotations

import pathlib
import struct

from ..interp.values import PracticeError


def load_image(path: str | pathlib.Path, kind: str, offset: int = 0) -> list[tuple[int, bytes]]:
    data = pathlib.Path(path).read_bytes()
    try:
        return _load(data, kind.lower(), offset)
    except (ValueError, IndexError, struct.error) as e:
        raise PracticeError(f"{pathlib.Path(path).name}: not a valid {kind} file ({e})") from None


def _load(data: bytes, kind: str, offset: int) -> list[tuple[int, bytes]]:
    if kind == "elf":
        segs = elf_segments(data)
    elif kind == "ihex":
        segs = ihex_segments(data.decode("ascii", errors="replace"))
    elif kind in ("srecord", "s3record", "srec"):
        segs = srec_segments(data.decode("ascii", errors="replace"))
    elif kind == "binary":
        segs = [(0, data)]
    else:
        raise PracticeError(f"image format {kind!r} not supported")
    return [(a + offset, b) for a, b in segs if b]


def elf_segments(data: bytes) -> list[tuple[int, bytes]]:
    if data[:4] != b"\x7fELF" or len(data) < 0x34:
        raise PracticeError("not an ELF file")
    is64, little = data[4] == 2, data[5] == 1
    e = "<" if little else ">"
    if is64:
        phoff, = struct.unpack_from(e + "Q", data, 0x20)
        phentsize, phnum = struct.unpack_from(e + "HH", data, 0x36)
    else:
        phoff, = struct.unpack_from(e + "I", data, 0x1C)
        phentsize, phnum = struct.unpack_from(e + "HH", data, 0x2A)
    out = []
    for k in range(phnum):
        o = phoff + k * phentsize
        if is64:
            p_type, _flags, p_offset, _vaddr, p_paddr, p_filesz = struct.unpack_from(e + "IIQQQQ", data, o)
        else:
            p_type, p_offset, _vaddr, p_paddr, p_filesz = struct.unpack_from(e + "IIIII", data, o)
        if p_type == 1 and p_filesz:          # PT_LOAD; load at the physical (LMA) address
            if p_offset + p_filesz > len(data):
                raise PracticeError("ELF segment runs past the end of the file")
            out.append((p_paddr, data[p_offset:p_offset + p_filesz]))
    return out


def elf_entry(data: bytes) -> int:
    e = "<" if data[5] == 1 else ">"
    return struct.unpack_from(e + ("Q" if data[4] == 2 else "I"), data, 0x18)[0]


def _merge(chunks: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    out: list[tuple[int, bytearray]] = []
    for a, b in sorted(chunks):
        if out and out[-1][0] + len(out[-1][1]) == a:
            out[-1][1].extend(b)
        else:
            out.append((a, bytearray(b)))
    return [(a, bytes(b)) for a, b in out]


def ihex_segments(text: str) -> list[tuple[int, bytes]]:
    base, chunks = 0, []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise PracticeError(f"Intel HEX line {n}: missing ':'")
        rec = bytes.fromhex(line[1:])
        if len(rec) < 5 or len(rec) != rec[0] + 5:
            raise PracticeError(f"Intel HEX line {n}: record length does not match its byte count")
        if sum(rec) & 0xFF:
            raise PracticeError(f"Intel HEX line {n}: checksum error")
        ln, addr, typ, payload = rec[0], (rec[1] << 8) | rec[2], rec[3], rec[4:4 + rec[0]]
        if typ == 0:
            chunks.append((base + addr, payload))
        elif typ == 1:
            break
        elif typ == 2:
            base = int.from_bytes(payload, "big") << 4
        elif typ == 4:
            base = int.from_bytes(payload, "big") << 16
    return _merge(chunks)


def srec_segments(text: str) -> list[tuple[int, bytes]]:
    chunks = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith("S") or len(line) < 4:
            raise PracticeError(f"S-record line {n}: bad record")
        t = line[1]
        rec = bytes.fromhex(line[2:])
        if not rec or len(rec) != rec[0] + 1:
            raise PracticeError(f"S-record line {n}: record length does not match its byte count")
        if (sum(rec) & 0xFF) != 0xFF:
            raise PracticeError(f"S-record line {n}: checksum error")
        alen = {"1": 2, "2": 3, "3": 4}.get(t)
        if alen:
            addr = int.from_bytes(rec[1:1 + alen], "big")
            chunks.append((addr, rec[1 + alen:-1]))
    return _merge(chunks)
