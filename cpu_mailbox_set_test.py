"""CPU OC mailbox SET round-trip test - DOWN-step then restore.

PROPOSED TEST - requires explicit human approval before running.

What it does, in order:
  1. Query 0x02 (GET_PER_CORE_RATIO_LIMIT) to record current limits.
  2. Query 0x10 (GET_VOLTAGE_FREQUENCY, domain 0) to record current VF data.
  3. SET 0x11 domain 0 with data.MaxOcRatio=30 (LOWER than stock 38x).
     Voltage fields are all zero = adaptive voltage, no offset (stock
     voltage behavior). Only the ratio cap changes, and only downward.
  4. Re-query 0x02 - expect the programmed limits to show 30s.
  5. SET 0x11 domain 0 with data.MaxOcRatio=38 (restore stock cap).
  6. Re-query 0x02 - expect 38/35 restored.

Why down-step first: lowering the cap cannot add heat or voltage stress.
If the SET path is locked, the write is rejected and we learn that without
ever increasing anything. If it works, apply+undo is proven on hardware.

Hard limits of this script:
  - Writes ONLY to MSR 0x150.
  - ONLY the two hardcoded commands below are possible: cap=30, cap=38.
  - No ratio above stock (38), no voltage changes, no other MSRs.

Requires admin. Caller must elevate explicitly - no self-elevation.

Usage:
    python cpu_mailbox_set_test.py --output PATH
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
BUSY_BIT = 1 << 63
DOMAIN_IA_CORE = 0
CMD_GET_PER_CORE_RATIO_LIMIT = 0x02
CMD_GET_VOLTAGE_FREQUENCY = 0x10
CMD_SET_VOLTAGE_FREQUENCY = 0x11

# The only two SET payloads this script can ever send.
STOCK_MAX_RATIO = 38
TEST_CAP_RATIO = 30


def mailbox_cmd(command, domain, data):
    return ((domain & 0xFF) << 40) | ((command & 0xFF) << 32) | BUSY_BIT | (data & 0xFFFFFFFF)


SET_STEPS = (
    ("down-step: cap ratios at 30x", mailbox_cmd(CMD_SET_VOLTAGE_FREQUENCY, DOMAIN_IA_CORE, TEST_CAP_RATIO)),
    ("restore: cap ratios back at 38x", mailbox_cmd(CMD_SET_VOLTAGE_FREQUENCY, DOMAIN_IA_CORE, STOCK_MAX_RATIO)),
)
ALLOWED_WRITES = frozenset(v for _, v in SET_STEPS)

# GET commands are also sent as writes to 0x150 (command word in,
# response read back), so the write guard must permit query requests.
ALLOWED_QUERY_CMDS = frozenset({CMD_GET_PER_CORE_RATIO_LIMIT, CMD_GET_VOLTAGE_FREQUENCY})


def read_msr(lib, handle, addr):
    in_buf = (ctypes.c_ulonglong * 1)(addr)
    out_buf = (ctypes.c_ulonglong * 1)(0)
    ret_size = ctypes.c_size_t(0)
    hr = lib.pawnio_execute(handle, b"ioctl_read_msr",
                            in_buf, 1, out_buf, 1, ctypes.byref(ret_size))
    if hr != 0:
        raise RuntimeError(f"read_msr 0x{addr:X} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    if ret_size.value != 1:
        raise RuntimeError(f"Expected 1 output cell, got {ret_size.value}")
    return out_buf[0]


def write_msr(lib, handle, addr, value):
    """Guarded write: only MSR 0x150, only the two pre-approved payloads."""
    if addr != MSR_OC_MAILBOX:
        raise ValueError(f"write to MSR 0x{addr:X} not allowed")
    is_query = (((value >> 32) & 0xFF) in ALLOWED_QUERY_CMDS
                and (value & 0xFFFFFFFF) == 0 and ((value >> 40) & 0xFF) == 0)
    if not is_query and value not in ALLOWED_WRITES:
        raise ValueError(f"mailbox payload 0x{value:016X} not in approved set")
    in_buf = (ctypes.c_ulonglong * 2)(addr, value)
    out_buf = (ctypes.c_ulonglong * 1)(0)
    ret_size = ctypes.c_size_t(0)
    hr = lib.pawnio_execute(handle, b"ioctl_write_msr",
                            in_buf, 2, out_buf, 1, ctypes.byref(ret_size))
    if hr != 0:
        raise RuntimeError(f"write_msr 0x{addr:X} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def setup_pawnio():
    if not os.path.exists(PAWNIOLIB_PATH):
        raise RuntimeError(f"PawnIOLib.dll not found at {PAWNIOLIB_PATH}")
    blob_candidates = [p for p in INTELMSR_BINS if os.path.exists(p)]
    if not blob_candidates:
        raise RuntimeError(f"No IntelMSR.bin found in: {INTELMSR_BINS}")
    lib = ctypes.OleDLL(PAWNIOLIB_PATH)
    lib.pawnio_version.argtypes = [ctypes.POINTER(wintypes.ULONG)]
    lib.pawnio_version.restype = ctypes.c_long
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
    version = wintypes.ULONG(0)
    hr = lib.pawnio_version(ctypes.byref(version))
    if hr != 0:
        raise RuntimeError(f"pawnio_version failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    print(f"PawnIO library version: {(version.value >> 16) & 0xFFFF}."
          f"{(version.value >> 8) & 0xFF}.{version.value & 0xFF}")
    handle = wintypes.HANDLE(0)
    hr = lib.pawnio_open(ctypes.byref(handle))
    if hr != 0 or not handle.value:
        raise RuntimeError(f"pawnio_open failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    try:
        loaded = None
        for path in blob_candidates:
            data = open(path, "rb").read()
            blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            hr = lib.pawnio_load(handle, blob, len(data))
            if hr == 0:
                loaded = path
                break
            print(f"  pawnio_load failed for {path}, trying next")
        if loaded is None:
            raise RuntimeError("pawnio_load failed for all module blobs")
        print(f"IntelMSR module loaded: {loaded}")
    except Exception:
        lib.pawnio_close(handle)
        raise
    return lib, handle


def mailbox_transact(lib, handle, command, domain=0, data=0):
    """Send one mailbox command word and poll for the response."""
    request = mailbox_cmd(command, domain, data)
    print(f"  Request : write MSR 0x150 = 0x{request:016X} (cmd 0x{command:02X}, domain {domain}, data 0x{data:08X})")
    write_msr(lib, handle, MSR_OC_MAILBOX, request)
    for attempt in range(3):
        time.sleep(0.000003)
        response = read_msr(lib, handle, MSR_OC_MAILBOX)
        print(f"  Response: read  MSR 0x150 = 0x{response:016X} (attempt {attempt + 1})")
        if not (response & BUSY_BIT):
            return response
        print("    bit63 still set - mailbox busy, retrying")
    return response


def show_ratio_limits(label, response):
    code = (response >> 32) & 0xFF
    data = response & 0xFFFFFFFF
    print(f"  [{label}] code=0x{code:02X} data=0x{data:08X} "
          f"-> per-core caps: {data & 0xFF}, {(data >> 8) & 0xFF}, {(data >> 16) & 0xFF}, {(data >> 24) & 0xFF}")
    return code, data


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
        print("=== CPU OC Mailbox SET round-trip test (down-step, then restore) ===")
        print("Approved scope: two writes to MSR 0x150 only -")
        print(f"  cmd 0x11 domain 0 data.MaxOcRatio={TEST_CAP_RATIO} (down), then {STOCK_MAX_RATIO} (restore).")
        print("Voltage fields are zero = adaptive/no offset (stock voltage).")
        print()
        try:
            admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            admin = False
        if not admin:
            print("ERROR: requires administrator. Run from an elevated terminal.")
            sys.exit(1)
        try:
            lib, handle = setup_pawnio()
        except Exception as e:
            print(f"FAILED to initialize PawnIO: {e}")
            sys.exit(1)
        try:
            print("--- baseline ---")
            r = mailbox_transact(lib, handle, CMD_GET_PER_CORE_RATIO_LIMIT)
            _, base = show_ratio_limits("baseline", r)
            r = mailbox_transact(lib, handle, CMD_GET_VOLTAGE_FREQUENCY)
            print(f"  [vf baseline] code=0x{(r >> 32) & 0xFF:02X} data=0x{r & 0xFFFFFFFF:08X}")
            print()
            for label, payload in SET_STEPS:
                print(f"--- {label} ---")
                write_msr(lib, handle, MSR_OC_MAILBOX, payload)
                print(f"  wrote 0x{payload:016X}")
                time.sleep(0.01)
                r = mailbox_transact(lib, handle, CMD_GET_PER_CORE_RATIO_LIMIT)
                show_ratio_limits("after write", r)
                print()
            print("--- final check ---")
            r = mailbox_transact(lib, handle, CMD_GET_PER_CORE_RATIO_LIMIT)
            _, final = show_ratio_limits("final", r)
            print()
        finally:
            lib.pawnio_close(handle)
        print("=== Summary ===")
        if final == base:
            print("Ratio limits restored to baseline. Round-trip complete.")
        else:
            print(f"WARNING: final limits 0x{final:08X} differ from baseline 0x{base:08X}")
            print("Manual check needed - reboot also restores defaults.")
    finally:
        if output_file:
            sys.stdout = sys.__stdout__
            output_file.close()
            print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
