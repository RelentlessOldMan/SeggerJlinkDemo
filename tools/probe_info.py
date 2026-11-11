"""Read-only J-Link probe info for the probe named in a target.yaml.

The probe is opened through cmmrun's J-Link backend, so every safety rule applies: the serial
must be in target.yaml, deny lists (target.yaml and ~/.cmmrun/probes.yaml) are honoured, on-board
probes are refused unless allowed, automatic firmware update is disabled before the probe is
opened, and the opened probe's serial is checked. It does not connect to the target.

usage: python tools/probe_info.py target.yaml
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmmrun.backends.jlink import JLinkBackend, load_config  # noqa: E402
from cmmrun.interp.values import TargetError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target", help="target.yaml naming the probe by serial")
    a = ap.parse_args()
    try:
        b = JLinkBackend(load_config(a.target))
        b._open(check_power=False)        # report VTref rather than refuse
    except TargetError as e:
        print(f"probe_info: {e}", file=sys.stderr)
        return 3
    jl = b.jl
    try:
        print("DLL version     :", jl.version)
        print("Product         :", jl.product_name)
        print("Serial          :", jl.serial_number)
        print("Hardware version:", jl.hardware_version)
        print("Firmware        :", jl.firmware_version)
        print("Firmware outdated vs DLL:", jl.firmware_outdated(), "(not updated automatically)")
        print(f"VTref           : {jl.hardware_status.voltage} mV")
        print("Features        :", ", ".join(jl.features) or "(none)")
    finally:
        b._close_probe()
    return 0


if __name__ == "__main__":
    sys.exit(main())
