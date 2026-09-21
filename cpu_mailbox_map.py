"""CPU OC mailbox capability map - QUERY commands only.

Sends a small batch of GET_* commands to the OC mailbox (MSR 0x150)
through the PawnIO IntelMSR module and prints raw + decoded responses.

Scope (hard limits):
  - Only GET/query command codes listed in ALLOWED_COMMANDS.
  - Each command is one write to MSR 0x150: ((busy|params|cmd) << 32).
  - NO SET commands, no ratio/voltage/limit changes.
  - Raw request/response values are printed for every command.

Command encoding (from pyhwinfo msrbox.py, leaked-BIOS struct
_OC_MAILBOX_FULL): 32-bit cmd dword = busy<<31 | p2<<16 | p1<<8 | cmd;
the 64-bit MSR value = (cmd_dword << 32) | data.

Requires admin. Caller must elevate explicitly - no self-elevation.

Usage:
    python cpu_mailbox_map.py --output PATH
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

# GET/query commands only. SET commands are deliberately absent.
ALLOWED_COMMANDS = frozenset({0x01, 0x02, 0x04, 0x10, 0x14, 0x16, 0x18, 0x1A})

CMD_NAMES = {
    0x01: "GET_OC_CAPABILITIES",
    0x02: "GET_PER_CORE_RATIO_LIMIT",
    0x04: "GET_VR_TOPOLOGY",
    0x10: "GET_VOLTAGE_FREQUENCY",
    0x14: "GET_MISC_GLOBAL_CONFIG",
    0x16: "GET_ICCMAX",
    0x18: "GET_MISC_TURBO_CONTROL",
    0x1A: "GET_AVX_RATIO_OFFSET",
}

DOMAIN_NAMES = {0: "IA_CORE", 1: "GT", 2: "RING", 3: "UNCORE", 4: "SA"}


def read_msr(lib, handle, addr):
    in_buf = (ctypes.c_ulonglong * 1)(addr)
    out_buf = (ctypes.c_ulonglong * 1)(0)
    ret_size = ctypes.c_size_t(0)
    hr = lib.pawnio_execute(handle, b"ioctl_read_msr",
                            in_buf, 1, out_buf, 1, ctypes.byref(ret_size))
    if hr != 0:
        raise RuntimeError(f"read 0x{addr:X} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    return out_buf[0]


def write_msr(lib, handle, addr, value):
    """Write one MSR. Guarded: only MSR 0x150, only query commands."""
    if addr != MSR_OC_MAILBOX:
        raise ValueError(f"write to MSR 0x{addr:X} not allowed")
    cmd_dword = (value >> 32) & 0xFFFFFFFF
    command = cmd_dword & 0xFF
    if command not in ALLOWED_COMMANDS or not (cmd_dword & 0x80000000):
        raise ValueError(f"mailbox command 0x{command:02X} not allowed")
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
    """Send one OC mailbox query; return response data or None on error."""
    cmd_dword = 0x80000000 | ((p2 & 0xFF) << 16) | ((p1 & 0xFF) << 8) | (cmd & 0xFF)
    request = (cmd_dword << 32) | (data & 0xFFFFFFFF)
    name = CMD_NAMES.get(cmd, f"0x{cmd:02X}")
    print(f">> {name} (p1={p1} p2={p2}) request=0x{request:016X}")
    try:
        write_msr(lib, handle, MSR_OC_MAILBOX, request)
    except Exception as e:
        print(f"   write rejected: {e}")
        return None
    for attempt in range(5):
        time.sleep(0.000003)
        resp = read_msr(lib, handle, MSR_OC_MAILBOX)
        if not (resp & BUSY_BIT):
            status = (resp >> 32) & 0xFFFFFFFF
            out = resp & 0xFFFFFFFF
            print(f"   response=0x{resp:016X} status=0x{status:X} data=0x{out:08X}")
            if status:
                print("   -> non-zero status: command NOT accepted")
                return None
            return out
    print("   mailbox stayed busy - timed out")
    return None


def s11(value):
    """Signed 11-bit -> volts (S11.0.10 fixed point)."""
    if value & (1 << 10):
        value -= (1 << 11)
    return round(value / 1024.0, 4)


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
        print("=== CPU OC Mailbox Map (QUERY commands only) ===")
        print("GET commands only - no ratio/voltage/limit changes.")
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
        try:
            vr_ia_addr = None

            print("--- VR topology ---")
            topo = mailbox(lib, handle, 0x04)
            if topo is not None:
                vr_ia_addr = (topo >> 8) & 0xF
                vr_gt_addr = (topo >> 13) & 0xF
                print(f"   VrIaAddress={vr_ia_addr} VrGtAddress={vr_gt_addr}")

            print()
            print("--- OC capabilities (confirm mailbox is live) ---")
            caps = mailbox(lib, handle, 0x01)
            if caps is not None:
                print(f"   MaxOcRatioLimit={caps & 0xFF} "
                      f"RatioOc={(caps >> 8) & 1} VoltOvrd={(caps >> 9) & 1} VoltOffs={(caps >> 10) & 1}")

            print()
            print("--- Per-core ratio limits (cores 0-5) ---")
            for core in range(6):
                d = mailbox(lib, handle, 0x02, p1=core)
                if d is not None:
                    print(f"   core{core}: max_ratio={d & 0xFF}x min_ratio={(d >> 8) & 0xFF}x")

            print()
            print("--- Voltage/frequency point 0 per domain ---")
            for domain in (0, 2, 4):
                d = mailbox(lib, handle, 0x10, p1=domain, p2=0)
                if d is not None:
                    print(f"   {DOMAIN_NAMES.get(domain, domain)}: MaxOcRatio={d & 0xFF}x "
                          f"Vtarget={(d >> 8) & 0xFFF} (~{((d >> 8) & 0xFFF) / 1024.0:.3f}V) "
                          f"mode={'OVERRIDE' if (d >> 20) & 1 else 'ADAPTIVE'} "
                          f"Voffset={s11((d >> 21) & 0x7FF)}V")

            print()
            print("--- IccMax ---")
            if vr_ia_addr is not None:
                d = mailbox(lib, handle, 0x16, p1=vr_ia_addr)
                if d is not None:
                    print(f"   IccMax={(d & 0x7FF) * 0.25:.2f}A unlimited={(d >> 31) & 1}")
            else:
                print("   skipped - VR topology did not return VrIaAddress")

            print()
            print("--- Misc turbo control / AVX offset ---")
            d = mailbox(lib, handle, 0x18)
            if d is not None:
                print(f"   MISC_TURBO_CONTROL raw=0x{d:08X}")
            d = mailbox(lib, handle, 0x1A)
            if d is not None:
                print(f"   AVX_RATIO_OFFSET raw=0x{d:08X} offset={(d >> 8) & 0xFF}x")
        finally:
            lib.pawnio_close(handle)
        print()
        print("=== Summary ===")
        print("Queries only. No clock, ratio, voltage, or limit changes were made.")
    finally:
        if output_file:
            sys.stdout = sys.__stdout__
            output_file.close()
            print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
