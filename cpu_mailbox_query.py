"""CPU OC mailbox query batch - QUERY commands only.

Sends approved QUERY commands to the OC mailbox (MSR 0x150) through the
PawnIO IntelMSR module, then reads each response.

Scope (hard limits):
  - Writes to MSR 0x150 only, and only (cmd << 32) | BUSY_BIT where cmd
    is in ALLOWED_COMMANDS. All listed commands are GET/QUERY style -
    they ask the CPU to report settings; they change nothing.
  - No SET commands, no other MSRs, no other commands.
  - Full raw request/response values are printed and saved.

Protocol (from Linux kernel drivers/platform/x86/intel/turbo_max_3.c):
  write (cmd << 32) | BIT(63) -> MSR 0x150, wait ~3us, read 0x150,
  bit63 still set = busy (retry, max 2), bits 39:32 nonzero = error,
  bits 31:0 = response data.

Requires admin. Caller must elevate explicitly - no self-elevation.

Usage:
    python cpu_mailbox_query.py --output PATH
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
CMD_GET_OC_CAPABILITIES = 0x01
# Approved QUERY batch (GET-style commands only - they report, never set):
#   0x02 GET_PER_CORE_RATIO_LIMIT, 0x10 GET_VOLTAGE_FREQUENCY (query),
#   0x16 query (per team plan), 0x18 GET_MISC_TURBO_CONTROL.
QUERY_COMMANDS = (0x01, 0x02, 0x10, 0x16, 0x18)
BUSY_BIT = 1 << 63
ALLOWED_WRITE_ADDRS = frozenset({MSR_OC_MAILBOX})
ALLOWED_COMMANDS = frozenset(QUERY_COMMANDS)


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
    """Write one MSR. Guarded: only MSR 0x150 and only the query command."""
    if addr not in ALLOWED_WRITE_ADDRS:
        raise ValueError(f"write to MSR 0x{addr:X} not allowed")
    command = (value >> 32) & 0xFF
    if command not in ALLOWED_COMMANDS or not (value & BUSY_BIT):
        raise ValueError(f"mailbox command 0x{command:02X} not allowed")
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


def mailbox_query(lib, handle, command):
    """Send one OC mailbox query and read the response. Returns raw value."""
    request = (command << 32) | BUSY_BIT
    print(f"  Request : write MSR 0x150 = 0x{request:016X} (cmd 0x{command:02X})")
    write_msr(lib, handle, MSR_OC_MAILBOX, request)
    for attempt in range(3):
        time.sleep(0.000003)
        response = read_msr(lib, handle, MSR_OC_MAILBOX)
        print(f"  Response: read  MSR 0x150 = 0x{response:016X} (attempt {attempt + 1})")
        if not (response & BUSY_BIT):
            return response
        print("    bit63 still set - mailbox busy, retrying")
    return response


def interpret_capabilities(response):
    """Best-effort interpretation per nwinfo issue 123. Raw values rule."""
    code = (response >> 32) & 0xFF
    data = response & 0xFFFFFFFF
    print(f"  Response code (bits 39:32) = 0x{code:02X}")
    if code != 0:
        print("    -> non-zero response code = command NOT accepted by CPU")
        return
    print("    -> zero = command accepted")
    print(f"  Response data (bits 31:0)  = 0x{data:08X}")
    print(f"    bits 7:0  MaxOcRatioLimit        = {data & 0xFF}")
    print(f"    bit  8    RatioOcSupported       = {(data >> 8) & 1}")
    print(f"    bit  9    VoltageOverridesSupport= {(data >> 9) & 1}")
    print(f"    bit  10   VoltageOffsetSupported = {(data >> 10) & 1}")
    print("  NOTE: interpretation is from community sources; raw value is authoritative.")


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
        print("=== CPU OC Mailbox Query Batch ===")
        print("Writes to MSR 0x150 for QUERY commands only: "
              + ", ".join(f"0x{c:02X}" for c in QUERY_COMMANDS))
        print("These are QUERIES - they change no CPU settings.")
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
            for command in QUERY_COMMANDS:
                print(f"--- command 0x{command:02X} ---")
                response = mailbox_query(lib, handle, command)
                print()
                if command == CMD_GET_OC_CAPABILITIES:
                    interpret_capabilities(response)
                else:
                    code = (response >> 32) & 0xFF
                    print(f"  Response code (bits 39:32) = 0x{code:02X}")
                    print(f"  Response data (bits 31:0)  = 0x{response & 0xFFFFFFFF:08X}")
                    if response & BUSY_BIT:
                        print("  WARNING: bit63 still set - mailbox stayed busy")
                print()
        finally:
            lib.pawnio_close(handle)
        print("=== Summary ===")
        print("Sent mailbox queries only. No clock, ratio, voltage, or limit changes.")
    finally:
        if output_file:
            sys.stdout = sys.__stdout__
            output_file.close()
            print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
