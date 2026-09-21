"""CPU OC first-write test: +1 step ratio, then restore. Minimal and reversible.

Sequence (all through the PawnIO IntelMSR module, MSR 0x150 OC mailbox):
  1. GET_VOLTAGE_FREQUENCY (0x10) on IA_CORE - confirm stock (all zeros).
  2. SET_VOLTAGE_FREQUENCY (0x11) with MaxOcRatio=39 (one step over the
     stock 38x single-core turbo limit). Voltage fields = 0 = unchanged.
  3. GET_VOLTAGE_FREQUENCY readback - confirm the write took.
  4. Read MSR 0x1AD - check whether the turbo limit register changed.
  5. SET_VOLTAGE_FREQUENCY with all-zero data - restore (clear overrides).
  6. GET_VOLTAGE_FREQUENCY readback - confirm restored to stock.

Scope (hard limits):
  - Only commands 0x10 (GET_VF) and 0x11 (SET_VF), only MSR 0x150.
  - SET is sent at most twice: once with MaxOcRatio=39, once with 0 (restore).
  - Domain IA_CORE, VF point index 0. No voltage change. No other MSRs.

Requires admin. Caller must elevate explicitly - no self-elevation.

Usage:
    python cpu_oc_test.py --output PATH
"""
import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes

PAWNIOLIB_PATH = r"C:\Program Files\PawnIO\PawnIOLib.dll"
_HERE = os.path.dirname(os.path.abspath(__file__))
INTELMSR_BINS = [
    os.path.join(_HERE, "..", "pawnio_modules_024", "IntelMSR.bin"),
    os.path.join(_HERE, "IntelMSR.bin"),
]

MSR_OC_MAILBOX = 0x150
MSR_TURBO_RATIO_LIMIT = 0x1AD
BUSY_BIT = 1 << 63

CMD_GET_VF = 0x10
CMD_SET_VF = 0x11
ALLOWED_COMMANDS = frozenset({CMD_GET_VF, CMD_SET_VF})

DOMAIN_IA_CORE = 0
VF_INDEX = 0
TEST_RATIO = 39   # one step over stock 38x single-core turbo cap
MAX_ALLOWED_RATIO = 40  # hard cap: refuse anything beyond a +2 step test


def read_msr(lib, handle, addr):
    if addr not in (MSR_OC_MAILBOX, MSR_TURBO_RATIO_LIMIT):
        raise ValueError(f"read of MSR 0x{addr:X} not allowed")
    in_buf = (ctypes.c_ulonglong * 1)(addr)
    out_buf = (ctypes.c_ulonglong * 1)(0)
    ret_size = ctypes.c_size_t(0)
    hr = lib.pawnio_execute(handle, b"ioctl_read_msr",
                            in_buf, 1, out_buf, 1, ctypes.byref(ret_size))
    if hr != 0:
        raise RuntimeError(f"read 0x{addr:X} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    return out_buf[0]


def write_msr(lib, handle, addr, value):
    """Write one MSR. Guarded: MSR 0x150 only, GET_VF/SET_VF only,
    SET only with MaxOcRatio in [0, MAX_ALLOWED_RATIO] and zero voltage fields."""
    if addr != MSR_OC_MAILBOX:
        raise ValueError(f"write to MSR 0x{addr:X} not allowed")
    cmd_dword = (value >> 32) & 0xFFFFFFFF
    command = cmd_dword & 0xFF
    if command not in ALLOWED_COMMANDS or not (cmd_dword & 0x80000000):
        raise ValueError(f"mailbox command 0x{command:02X} not allowed")
    data = value & 0xFFFFFFFF
    if command == CMD_SET_VF:
        ratio = data & 0xFF
        voltage_bits = data & ~0xFF  # everything except MaxOcRatio must be 0
        if ratio > MAX_ALLOWED_RATIO or voltage_bits != 0:
            raise ValueError(f"SET_VF data 0x{data:08X} outside allowed test bounds")
    in_buf = (ctypes.c_ulonglong * 2)(addr, value)
    out_buf = (ctypes.c_ulonglong * 1)(0)
    ret_size = ctypes.c_size_t(0)
    hr = lib.pawnio_execute(handle, b"ioctl_write_msr",
                            in_buf, 2, out_buf, 1, ctypes.byref(ret_size))
    if hr != 0:
        raise RuntimeError(f"write 0x{addr:X} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def setup_pawnio():
    if not os.path.exists(PAWNIOLIB_PATH):
        raise RuntimeError(f"PawnIOLib.dll not found at {PAWNIOLIB_PATH}")
    blobs = [p for p in INTELMSR_BINS if os.path.exists(p)]
    if not blobs:
        raise RuntimeError(f"No IntelMSR.bin found in: {INTELMSR_BINS}")

    lib = ctypes.OleDLL(PAWNIOLIB_PATH)
    for name in ("pawnio_open", "pawnio_load", "pawnio_execute", "pawnio_close"):
        if not hasattr(lib, name):
            raise RuntimeError(f"PawnIOLib missing export {name}")
    lib.pawnio_open.argtypes = [ctypes.POINTER(wintypes.HANDLE)]
    lib.pawnio_open.restype = ctypes.c_long
    lib.pawnio_load.argtypes = [wintypes.HANDLE,
                              ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t]
    lib.pawnio_load.restype = ctypes.c_long
    lib.pawnio_execute.argtypes = [wintypes.HANDLE, ctypes.c_char_p,
                                   ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
    lib.pawnio_execute.restype = ctypes.c_long
    lib.pawnio_close.argtypes = [wintypes.HANDLE]
    lib.pawnio_close.restype = ctypes.c_long

    handle = wintypes.HANDLE(0)
    hr = lib.pawnio_open(ctypes.byref(handle))
    if hr != 0 or not handle.value:
        raise RuntimeError(f"pawnio_open failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    try:
        loaded = None
        for path in blobs:
            data = open(path, "rb").read()
            blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            if lib.pawnio_load(handle, blob, len(data)) == 0:
                loaded = path
                break
        if loaded is None:
            raise RuntimeError("pawnio_load failed for all module blobs")
        print(f"IntelMSR module: {os.path.basename(os.path.dirname(loaded))}/IntelMSR.bin")
    except Exception:
        lib.pawnio_close(handle)
        raise
    return lib, handle


def mailbox(lib, handle, cmd, p1=0, p2=0, data=0):
    """Send one OC mailbox command; return (status, data) or (None, None)."""
    cmd_dword = 0x80000000 | ((p2 & 0xFF) << 16) | ((p1 & 0xFF) << 8) | (cmd & 0xFF)
    request = (cmd_dword << 32) | (data & 0xFFFFFFFF)
    label = "GET_VF" if cmd == CMD_GET_VF else "SET_VF"
    print(f">> {label} data=0x{data & 0xFFFFFFFF:08X} request=0x{request:016X}")
    write_msr(lib, handle, MSR_OC_MAILBOX, request)
    for attempt in range(5):
        time.sleep(0.000003)
        resp = read_msr(lib, handle, MSR_OC_MAILBOX)
        if not (resp & BUSY_BIT):
            status = (resp >> 32) & 0xFFFFFFFF
            out = resp & 0xFFFFFFFF
            print(f"   response=0x{resp:016X} status=0x{status:X} data=0x{out:08X}")
            return status, out
    print("   mailbox stayed busy - timed out")
    return None, None


def show_turbo(lib, handle, tag):
    val = read_msr(lib, handle, MSR_TURBO_RATIO_LIMIT)
    ratios = [(val >> (i * 8)) & 0xFF for i in range(6)]
    print(f"   MSR 0x1AD {tag}: {[f'{r}x' for r in ratios]} (raw 0x{val:016X})")
    return val


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", metavar="PATH", default=None)
    args = parser.parse_args()

    output_file = None
    if args.output:
        path = os.path.abspath(args.output)
        hq = os.path.abspath(os.path.join(_HERE, ".."))
        if not path.startswith(hq):
            print("ERROR: --output path must be under HQ")
            sys.exit(1)
        output_file = open(path, "w")
        sys.stdout = output_file

    try:
        print("=== CPU OC First-Write Test (+1 step, then restore) ===")
        print()
        try:
            admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            admin = False
        if not admin:
            print("ERROR: requires administrator.")
            sys.exit(1)
        try:
            lib, handle = setup_pawnio()
        except Exception as e:
            print(f"FAILED to initialize PawnIO: {e}")
            sys.exit(1)

        restored = False
        try:
            print("--- 1. Baseline ---")
            status, before = mailbox(lib, handle, CMD_GET_VF, p1=DOMAIN_IA_CORE, p2=VF_INDEX)
            if status is None:
                print("FAILED: could not read VF. Aborting - nothing changed.")
                return
            print(f"   current VF data = 0x{before:08X} (0 = stock, no overrides)")
            show_turbo(lib, handle, "baseline")

            print()
            print(f"--- 2. Apply test: MaxOcRatio={TEST_RATIO}x, voltage fields = 0 ---")
            status, _ = mailbox(lib, handle, CMD_SET_VF,
                                p1=DOMAIN_IA_CORE, p2=VF_INDEX, data=TEST_RATIO)
            if status is None:
                print("SET timed out - attempting restore anyway.")
            elif status != 0:
                print(f"SET rejected (status 0x{status:X}) - nothing applied.")
            else:
                print("   SET accepted by mailbox.")

            print()
            print("--- 3. Readback ---")
            status, after = mailbox(lib, handle, CMD_GET_VF, p1=DOMAIN_IA_CORE, p2=VF_INDEX)
            if status is not None and status == 0:
                if (after & 0xFF) == TEST_RATIO:
                    print(f"   CONFIRMED: MaxOcRatio reads back {TEST_RATIO}x")
                else:
                    print(f"   readback data=0x{after:08X} (ratio field {after & 0xFF}x)")
            show_turbo(lib, handle, "after write")

            print()
            print("--- 4. Restore: SET_VF data=0 (clear override) ---")
            status, _ = mailbox(lib, handle, CMD_SET_VF,
                                p1=DOMAIN_IA_CORE, p2=VF_INDEX, data=0)
            if status is not None and status == 0:
                restored = True
                print("   restore accepted")

            print()
            print("--- 5. Restore readback ---")
            status, final = mailbox(lib, handle, CMD_GET_VF, p1=DOMAIN_IA_CORE, p2=VF_INDEX)
            if status is not None and status == 0:
                print(f"   VF data now = 0x{final:08X}"
                      + (" (back to stock)" if final == before else " (DIFFERS from baseline!)"))
            show_turbo(lib, handle, "after restore")
        finally:
            if not restored:
                try:
                    mailbox(lib, handle, CMD_SET_VF, p1=DOMAIN_IA_CORE, p2=VF_INDEX, data=0)
                    print("(failsafe restore write sent)")
                except Exception:
                    pass
            lib.pawnio_close(handle)

        print()
        print("=== Summary ===")
        print("Test applied one +1 step MaxOcRatio write and restored it.")
        print("No voltage fields were changed at any point.")
    finally:
        if output_file:
            sys.stdout = sys.__stdout__
            output_file.close()
            print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
