"""Read-only J-Link probe info. Opens ONLY the probe given by serial number,
with automatic firmware update disabled. Does not connect to a target.

usage: python tools/probe_info.py <serial> [--dll PATH] [--deny SERIAL ...]
"""
import argparse
import sys

import pylink


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("serial", type=int)
    ap.add_argument("--dll", help="path to JLink_x64.dll")
    ap.add_argument("--deny", type=int, nargs="*", default=[])
    a = ap.parse_args()

    if a.serial in a.deny:
        print(f"refusing: serial {a.serial} is on the deny list", file=sys.stderr)
        return 3

    lib = pylink.Library(a.dll) if a.dll else None
    jl = pylink.JLink(lib=lib) if lib else pylink.JLink()
    jl.disable_dialog_boxes()

    print("DLL version     :", jl.version)
    # Must run before open(): otherwise the DLL may reflash probe firmware.
    jl.exec_command("DisableAutoUpdateFW")

    jl.open(serial_no=a.serial)
    try:
        print("Product         :", jl.product_name)
        print("Serial          :", jl.serial_number)
        print("Hardware version:", jl.hardware_version)
        print("Firmware        :", jl.firmware_version)
        print("Firmware outdated vs DLL:", jl.firmware_outdated())
        st = jl.hardware_status
        print(f"VTref           : {st.voltage} mV")
        print("Capabilities    :", hex(jl.capabilities & 0xFFFFFFFF))
        print("Features        :", ", ".join(jl.features) or "(none)")
    finally:
        jl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
